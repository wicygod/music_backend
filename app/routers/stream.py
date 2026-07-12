import asyncio
import contextlib
import hashlib
import os
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse

import httpx
import yt_dlp
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.track import Track
from app.repositories.tracks import get_track
from app.services.proxy_rotator import proxy_rotator


router = APIRouter(prefix="/api", tags=["stream"])

STREAM_CACHE_TTL_SECONDS = 15 * 60
STREAM_CACHE_REFRESH_MARGIN_SECONDS = 60
HLS_STREAM_CACHE_TTL_SECONDS = 60
MP3_BITRATE_KBPS = 192
AUDIO_CACHE_DIR = Path(
    os.getenv("MUSIC_AUDIO_CACHE_DIR", str(Path(tempfile.gettempdir()) / "music_backend_audio_cache"))
)
AUDIO_CACHE_MAX_BYTES = max(
    64 * 1024 * 1024,
    int(os.getenv("MUSIC_AUDIO_CACHE_MAX_BYTES", str(512 * 1024 * 1024))),
)
MIN_CACHED_AUDIO_BYTES = 16 * 1024
_stream_cache: dict[str, tuple[float, str]] = {}
_audio_cache_tasks: dict[Path, asyncio.Task[Path]] = {}
HLS_URI_RE = re.compile(r'URI="([^"]+)"')
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
COOKIES_FILE = Path("secrets/cookies.txt")
UPSTREAM_HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
SEARCH_PROVIDERS = (
    {"name": "soundcloud", "search": "scsearch5:{query}"},
    {"name": "youtube", "search": "https://music.youtube.com/search?q={query}"},
)
CORS_HEADERS = {
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
MP3_HEADERS = {
    **CORS_HEADERS,
    "Accept-Ranges": "bytes",
    "X-Content-Type-Options": "nosniff",
}
BAD_VIDEO_TERMS_RE = re.compile(
    r"\b("
    r"reaction|review|tutorial|podcast|interview|vlog|blog|lets\s*play|let'?s\s*play|gameplay|"
    r"walkthrough|stream|live\s*stream|news|politics|mock(?:s|ed|ing)?|blast(?:s|ed|ing)?|"
    r"claim(?:s|ed|ing)?|humiliation|hollywood|grammys|ai\s+music\s+video|ai\s+cover|"
    r"relationship|robbed|bizarre|insecurity|celebrity|scandal|"
    r"обзор|реакц(?:ия|ии)|прохожд(?:ение|ения)|летсплей|стрим"
    r")\b",
    re.IGNORECASE,
)
MAX_MUSIC_DURATION_SECONDS = 15 * 60


def _log(message: str) -> None:
    print(message, flush=True)


def _pick_user_agent() -> str:
    return random.choice(USER_AGENTS)


def _upstream_headers() -> dict[str, str]:
    headers = dict(UPSTREAM_HEADERS)
    headers["User-Agent"] = _pick_user_agent()
    return headers


def _yt_dlp_options(**overrides) -> dict:
    headers = _upstream_headers()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "http_headers": headers,
    }
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        options["cookiefile"] = str(COOKIES_FILE)
    proxy = proxy_rotator.next_proxy()
    if proxy:
        options["proxy"] = proxy
    options.update(overrides)
    return options


def _source_host(source_url: str | None) -> str:
    if not source_url:
        return ""
    return (urlparse(str(source_url)).hostname or "").lower().removeprefix("www.")


def _is_soundcloud_source(source_url: str | None) -> bool:
    host = _source_host(source_url)
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def _youtube_video_id(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = urlparse(str(source_url))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
        video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        return video_id or None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return video_id or None
    return None


def _canonical_youtube_music_url(source_url: str | None, external_id: str | None = None) -> str | None:
    video_id = _youtube_video_id(source_url) or (external_id if external_id and not external_id.isdigit() else None)
    if not video_id:
        return None
    return f"https://music.youtube.com/watch?v={video_id}"


def _is_youtube_music_source(source_url: str | None) -> bool:
    return _youtube_video_id(source_url) is not None


def _entry_source_url(entry: dict) -> str | None:
    source_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if _is_youtube_music_source(source_url):
        return _canonical_youtube_music_url(str(source_url), str(entry.get("id") or ""))
    if _is_soundcloud_source(source_url):
        return str(source_url)
    return None


def _entry_is_music_candidate(provider_name: str, entry: dict) -> bool:
    source_url = _entry_source_url(entry)
    if not source_url:
        return False

    duration = int(entry.get("duration") or 0)
    if duration and duration > MAX_MUSIC_DURATION_SECONDS:
        return False

    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "creator", "description", "webpage_url", "url")
    )
    if BAD_VIDEO_TERMS_RE.search(haystack):
        return False

    if provider_name == "soundcloud":
        return _is_soundcloud_source(source_url)

    if provider_name == "youtube":
        if not _is_youtube_music_source(source_url):
            return False
        categories = " ".join(str(item) for item in (entry.get("categories") or []))
        channel = str(entry.get("channel") or entry.get("uploader") or "")
        title = str(entry.get("title") or "")
        music_markers = (
            "music" in categories.lower()
            or "music.youtube.com" in str(entry.get("webpage_url") or entry.get("url") or "").lower()
            or channel.lower().endswith(" - topic")
            or "official audio" in title.lower()
            or "official music video" in title.lower()
        )
        return music_markers

    return False


def _is_hls_url(url: str) -> bool:
    return ".m3u8" in url.lower()


def _stream_url_expires_at(stream_url: str) -> float | None:
    raw_expires = None
    for key, value in parse_qs(urlparse(stream_url).query).items():
        if key.lower() in {"expires", "expire"}:
            raw_expires = value
            break
    if not raw_expires:
        return None
    try:
        return float(raw_expires[0])
    except (TypeError, ValueError):
        return None


def _cache_ttl_for_stream(stream_url: str) -> float:
    ttl = float(STREAM_CACHE_TTL_SECONDS)
    if _is_hls_url(stream_url):
        ttl = min(ttl, float(HLS_STREAM_CACHE_TTL_SECONDS))
    expires_at = _stream_url_expires_at(stream_url)
    if expires_at:
        ttl = min(ttl, max(0.0, expires_at - time.time() - STREAM_CACHE_REFRESH_MARGIN_SECONDS))
    return ttl


def _extract_stream_url(source_url: str) -> str:
    _log(f"[STREAM] Requested track URL: {source_url}")
    options = _yt_dlp_options(format="bestaudio/best")
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=False)

    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    direct_url = info.get("url")
    if direct_url:
        protocol = str(info.get("protocol") or "")
        ext = str(info.get("ext") or "")
        format_id = str(info.get("format_id") or "unknown")
        kind = "HLS" if ".m3u8" in str(direct_url).lower() or "m3u8" in protocol else ext.upper() or "audio"
        _log(f"[STREAM] yt-dlp selected format={format_id}, protocol={protocol}, type={kind}")
        return str(direct_url)

    formats = info.get("formats") or []
    audio_formats = [fmt for fmt in formats if fmt.get("url") and fmt.get("vcodec") in (None, "none")]
    if audio_formats:
        selected = audio_formats[-1]
        stream_url = str(selected["url"])
        protocol = str(selected.get("protocol") or "")
        ext = str(selected.get("ext") or "")
        kind = "HLS" if ".m3u8" in stream_url.lower() or "m3u8" in protocol else ext.upper() or "audio"
        _log(f"[STREAM] yt-dlp fallback format={selected.get('format_id', 'unknown')}, protocol={protocol}, type={kind}")
        return stream_url

    raise HTTPException(status_code=404, detail="Playable audio stream not found")


def _track_artist_names(track: Track) -> list[str]:
    links = sorted(track.artist_links, key=lambda link: 0 if link.role == "main" else 1)
    return [link.artist.name for link in links if link.artist and link.artist.name]


def _track_query(track: Track) -> str:
    artists = _track_artist_names(track)
    if artists:
        return f"{artists[0]} {track.title}".strip()
    return track.title.strip()


def _score_search_entry(query: str, entry: dict) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "description", "webpage_url", "url")
    ).lower()
    tokens = [token for token in re.split(r"\W+", query.lower()) if len(token) > 1]
    return sum(1 for token in tokens if token in haystack)


def _search_playable_source(
    query: str,
    *,
    skip_providers: set[str] | None = None,
    skip_urls: set[str] | None = None,
) -> tuple[str, str]:
    options = _yt_dlp_options(extract_flat=True)
    errors: list[str] = []
    skip_providers = skip_providers or set()
    skip_urls = {url.lower() for url in (skip_urls or set())}
    for provider in SEARCH_PROVIDERS:
        provider_name = provider["name"]
        if provider_name in skip_providers:
            continue
        search_query = provider["search"].format(query=quote_plus(query))
        _log(f"[STREAM SEARCH] Searching {provider_name}: {query}")
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(search_query, download=False)
        except Exception as exc:
            errors.append(f"{provider_name}: {exc}")
            _log(f"[STREAM SEARCH ERROR] {provider_name} failed for {query}: {exc}")
            continue

        entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
        playable = [
            entry
            for entry in entries
            if _entry_is_music_candidate(provider_name, entry)
        ]
        if not playable:
            continue

        if provider_name != "youtube":
            playable.sort(key=lambda entry: _score_search_entry(query, entry), reverse=True)
        for selected in playable:
            source_url = _entry_source_url(selected)
            if not source_url or str(source_url).lower() in skip_urls:
                continue
            title = selected.get("title") or "unknown"
            _log(f"[STREAM SEARCH] Selected {provider_name}: {title} -> {source_url}")
            return str(source_url), provider_name

    detail = "; ".join(errors) if errors else "No playable search result"
    raise HTTPException(status_code=404, detail=f"Playable source not found: {detail}")


def _is_known_stream_source(source_url: str | None) -> bool:
    return _is_soundcloud_source(source_url) or _is_youtube_music_source(source_url)


async def _resolve_track_source(
    track: Track,
    db: Session,
    *,
    force_search: bool = False,
    skip_providers: set[str] | None = None,
    skip_urls: set[str] | None = None,
) -> str:
    if not force_search:
        provider_url = _provider_source_url(track)
        if provider_url:
            return provider_url

    query = _track_query(track)
    if not query:
        raise HTTPException(status_code=404, detail="Track has no searchable title")

    source_url, provider = await run_in_threadpool(
        lambda: _search_playable_source(query, skip_providers=skip_providers, skip_urls=skip_urls)
    )
    track.source_url = source_url
    track.source_name = provider
    track.is_playable = True
    db.add(track)
    db.commit()
    _log(f"[STREAM TRACK] Saved playable source for track_id={track.id}: {source_url}")
    return source_url


def _provider_source_url(track: Track) -> str | None:
    source_name = (track.source_name or "").lower()
    source_url = str(track.source_url or "")
    external_id = str(track.source_external_id or "").strip()

    if source_name in {"youtube", "youtube_music", "yt"}:
        if "youtube.com/watch" in source_url or "youtu.be/" in source_url:
            return _canonical_youtube_music_url(source_url, external_id) or source_url
        if external_id and not external_id.isdigit():
            return f"https://music.youtube.com/watch?v={external_id}"

    if source_name in {"soundcloud", "sc"}:
        if "soundcloud.com" in source_url and "api-v2.soundcloud.com" not in source_url:
            return source_url

    if _is_known_stream_source(source_url):
        return source_url
    return None


async def _get_cached_stream_url(source_url: str) -> str:
    now = time.monotonic()
    cached = _stream_cache.get(source_url)
    if cached and now - cached[0] < _cache_ttl_for_stream(cached[1]):
        _log(f"[STREAM] Cache hit: {source_url}")
        return cached[1]

    try:
        stream_url = await run_in_threadpool(_extract_stream_url, source_url)
    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as exc:
        text = str(exc).lower()
        if "geo" in text or "country" in text or "region" in text or "not available" in text:
            _log(f"[STREAM ERROR] Трек заблокирован для текущего IP: {source_url} ({exc})")
        else:
            _log(f"[STREAM ERROR] yt-dlp failed for {source_url}: {exc}")
        raise HTTPException(status_code=502, detail=f"Could not extract stream: {exc}") from exc
    except Exception as exc:
        _log(f"[STREAM ERROR] Unexpected extraction error for {source_url}: {exc}")
        raise HTTPException(status_code=500, detail="Unexpected stream extraction error") from exc

    _stream_cache[source_url] = (now, stream_url)
    return stream_url


async def _validate_stream_url(stream_url: str) -> None:
    headers = _upstream_headers()
    if not _is_hls_url(stream_url):
        headers["Range"] = "bytes=0-0"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0, headers=headers) as client:
            async with client.stream("GET", stream_url) as response:
                _log(
                    f"[STREAM CHECK] Upstream status={response.status_code}, "
                    f"type={response.headers.get('content-type', 'unknown')}"
                )
                if response.status_code in {403, 404, 410}:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Cached stream URL expired or unavailable: HTTP {response.status_code}",
                    )
                response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Stream URL check failed: HTTP {exc.response.status_code}",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Stream URL check failed: {exc}") from exc


async def _get_valid_stream_url(source_url: str, *, refresh: bool = False) -> str:
    if refresh:
        _stream_cache.pop(source_url, None)
    stream_url = await _get_cached_stream_url(source_url)
    await _validate_stream_url(stream_url)
    return stream_url


async def proxy_stream(stream_url: str):
    _log(f"[STREAM PROXY] Segment/audio request: {stream_url}")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None, headers=_upstream_headers()) as client:
            async with client.stream("GET", stream_url) as response:
                _log(
                    f"[STREAM PROXY] Upstream status={response.status_code}, "
                    f"type={response.headers.get('content-type', 'unknown')}"
                )
                if response.status_code in {403, 404}:
                    _log(f"[STREAM ERROR] Segment HTTP {response.status_code}: {stream_url}")
                response.raise_for_status()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    if chunk:
                        yield chunk
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        _log(f"[STREAM ERROR] HTTP {status} while downloading segment/audio: {stream_url}")
        raise
    except httpx.HTTPError as exc:
        _log(f"[STREAM ERROR] Network error while downloading segment/audio: {stream_url} ({exc})")
        raise


async def proxy_hls_playlist(stream_url: str):
    _log(f"[STREAM HLS] Playlist request: {stream_url}")
    async with httpx.AsyncClient(follow_redirects=True, timeout=None, headers=_upstream_headers()) as client:
        response = await client.get(stream_url)
        _log(
            f"[STREAM HLS] Upstream playlist status={response.status_code}, "
            f"type={response.headers.get('content-type', 'unknown')}, final_url={response.url}"
        )
        if response.status_code in {403, 404}:
            _log(f"[STREAM ERROR] Playlist HTTP {response.status_code}: {stream_url}")
        response.raise_for_status()
        rewritten_count = 0
        for raw_line in response.text.splitlines(keepends=True):
            stripped = raw_line.strip()
            if not stripped:
                yield raw_line.encode("utf-8")
                continue
            if stripped.startswith("#"):
                rewritten_line, count = _rewrite_hls_uri_attributes(raw_line, str(response.url))
                rewritten_count += count
                yield rewritten_line.encode("utf-8")
                continue
            absolute_url = urljoin(str(response.url), stripped)
            proxied_url = _proxied_segment_url(absolute_url)
            line_ending = "\r\n" if raw_line.endswith("\r\n") else "\n" if raw_line.endswith("\n") else ""
            rewritten_count += 1
            yield f"{proxied_url}{line_ending}".encode("utf-8")
        _log(f"[STREAM HLS] Playlist rewritten successfully: {rewritten_count} URL(s)")


def _proxied_segment_url(absolute_url: str) -> str:
    return f"/api/stream/proxy?segment_url={quote(absolute_url, safe='')}"


def _rewrite_hls_uri_attributes(line: str, base_url: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        uri = match.group(1)
        if uri.startswith("data:"):
            return match.group(0)
        count += 1
        absolute_url = urljoin(base_url, uri)
        return f'URI="{_proxied_segment_url(absolute_url)}"'

    return HLS_URI_RE.sub(replace, line), count


def _media_type_for(stream_url: str) -> str:
    parsed = urlparse(stream_url)
    upstream_mime = (parse_qs(parsed.query).get("mime") or [None])[0]
    if upstream_mime and (upstream_mime.startswith("audio/") or upstream_mime.startswith("video/")):
        return upstream_mime

    url = parsed.path.lower()
    if url.endswith(".m3u8"):
        return "application/x-mpegURL"
    if url.endswith(".aac"):
        return "audio/aac"
    if url.endswith(".webm"):
        return "audio/webm"
    if url.endswith(".mp4") or url.endswith(".m4a") or url.endswith(".m4s") or url.endswith(".ts"):
        return "video/mp2t" if url.endswith(".ts") else "audio/mp4"
    return "audio/mpeg"


def _ffmpeg_headers_arg(headers: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def _audio_cache_path(cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return AUDIO_CACHE_DIR / f"{digest}.mp3"


def _cached_audio_is_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= MIN_CACHED_AUDIO_BYTES
    except OSError:
        return False


def _trim_audio_cache(protected_path: Path) -> None:
    try:
        entries = sorted(
            (item for item in AUDIO_CACHE_DIR.glob("*.mp3") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return

    total_bytes = 0
    for entry in entries:
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        total_bytes += size
        if total_bytes <= AUDIO_CACHE_MAX_BYTES or entry == protected_path:
            continue
        with contextlib.suppress(OSError):
            entry.unlink()


async def _build_cached_mp3(stream_url: str, cache_path: Path) -> Path:
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".part")
    with contextlib.suppress(OSError):
        temp_path.unlink()

    headers = _upstream_headers()
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise HTTPException(status_code=503, detail="ffmpeg is not installed on backend host")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-user_agent",
        headers["User-Agent"],
        "-headers",
        _ffmpeg_headers_arg(headers),
        "-i",
        stream_url,
        "-vn",
        "-map_metadata",
        "-1",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        f"{MP3_BITRATE_KBPS}k",
        "-f",
        "mp3",
        "-y",
        str(temp_path),
    ]
    _log(f"[STREAM CACHE] Building seekable MP3: {cache_path.name}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = await process.stderr.read() if process.stderr else b""
    return_code = await process.wait()
    if return_code != 0 or not _cached_audio_is_valid(temp_path):
        with contextlib.suppress(OSError):
            temp_path.unlink()
        message = stderr.decode("utf-8", errors="replace").strip()
        _log(f"[STREAM CACHE ERROR] ffmpeg rc={return_code}: {message[-2000:]}")
        raise HTTPException(status_code=502, detail="Unable to prepare seekable audio")

    os.replace(temp_path, cache_path)
    _trim_audio_cache(cache_path)
    _log(f"[STREAM CACHE] Ready: {cache_path.name} ({cache_path.stat().st_size} bytes)")
    return cache_path


async def _ensure_cached_mp3(stream_url: str, cache_key: str) -> Path:
    cache_path = _audio_cache_path(cache_key)
    if _cached_audio_is_valid(cache_path):
        with contextlib.suppress(OSError):
            os.utime(cache_path, None)
        return cache_path

    task = _audio_cache_tasks.get(cache_path)
    if task is None or task.done():
        task = asyncio.create_task(_build_cached_mp3(stream_url, cache_path))
        _audio_cache_tasks[cache_path] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _audio_cache_tasks.get(cache_path) is task:
            _audio_cache_tasks.pop(cache_path, None)


async def _stream_direct_url(
    stream_url: str,
    request: Request,
    *,
    start: float = 0.0,
    duration_seconds: int | None = None,
    cache_key: str | None = None,
) -> FileResponse:
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="ffmpeg is not installed on backend host")
    # WebView2 cannot reliably seek inside a live transcoding pipe: every byte
    # range starts a new FFmpeg process and the media stack keeps probing nearby
    # offsets. Build one complete constant-bitrate file, then let FileResponse
    # serve real RFC 7233 ranges from disk.
    effective_key = cache_key or f"stream:{stream_url}"
    cache_path = await _ensure_cached_mp3(stream_url, effective_key)
    return FileResponse(
        cache_path,
        media_type="audio/mpeg",
        headers=MP3_HEADERS,
    )


def _is_retryable_stream_error(exc: HTTPException) -> bool:
    return exc.status_code in {404, 500, 502}


async def _refresh_track_source_and_stream(
    track: Track,
    db: Session,
    failed_sources: set[str],
) -> tuple[str, str]:
    attempts: list[set[str]] = [set()]
    if any("soundcloud.com" in source.lower() for source in failed_sources):
        attempts.append({"soundcloud"})

    last_error: HTTPException | None = None
    for skip_providers in attempts:
        try:
            source_url = await _resolve_track_source(
                track,
                db,
                force_search=True,
                skip_providers=skip_providers,
                skip_urls=failed_sources,
            )
            stream_url = await _get_valid_stream_url(source_url, refresh=True)
            return source_url, stream_url
        except HTTPException as exc:
            last_error = exc
            if not _is_retryable_stream_error(exc):
                raise
            if track.source_url:
                failed_sources.add(str(track.source_url))
                _stream_cache.pop(str(track.source_url), None)
            _log(
                f"[STREAM TRACK] Refresh candidate failed for track_id={track.id}; "
                f"skip_providers={sorted(skip_providers)}; error={exc.detail}"
            )

    raise last_error or HTTPException(status_code=404, detail="Playable source not found")


async def _resolve_track_stream(
    track: Track,
    db: Session,
    source_url: str | None = None,
) -> tuple[str, str]:
    """Resolve a playable upstream while preserving the stream retry policy."""
    source_url = source_url or await _resolve_track_source(track, db)
    try:
        stream_url = await _get_valid_stream_url(source_url)
    except HTTPException as exc:
        if not _is_retryable_stream_error(exc):
            raise
        _log(
            f"[STREAM TRACK] Existing stream failed for track_id={track.id}; "
            f"refreshing stream URL: {source_url} ({exc.detail})"
        )
        try:
            stream_url = await _get_valid_stream_url(source_url, refresh=True)
        except HTTPException as refresh_exc:
            if not _is_retryable_stream_error(refresh_exc):
                raise
            _log(
                f"[STREAM TRACK] Source refresh failed for track_id={track.id}; "
                f"searching replacement source: {source_url} ({refresh_exc.detail})"
            )
            source_url, stream_url = await _refresh_track_source_and_stream(track, db, {source_url})
    return source_url, stream_url


def _track_audio_cache_key(track_id: int, source_url: str) -> str:
    return f"track:{track_id}:{source_url}"


@router.get("/stream")
async def stream(
    request: Request,
    url: str = Query(..., min_length=1),
    start: float = Query(0.0, ge=0),
) -> FileResponse:
    stream_url = await _get_valid_stream_url(url)
    return await _stream_direct_url(stream_url, request, start=start, cache_key=f"source:{url}")


@router.get("/stream/track/{track_id}")
async def stream_track(
    track_id: int,
    request: Request,
    start: float = Query(0.0, ge=0),
    db: Session = Depends(get_db),
) -> FileResponse:
    track = get_track(db, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    source_url = await _resolve_track_source(track, db)
    cache_key = _track_audio_cache_key(track.id, source_url)
    cache_path = _audio_cache_path(cache_key)
    if _cached_audio_is_valid(cache_path):
        with contextlib.suppress(OSError):
            os.utime(cache_path, None)
        return FileResponse(cache_path, media_type="audio/mpeg", headers=MP3_HEADERS)

    source_url, stream_url = await _resolve_track_stream(track, db, source_url)
    return await _stream_direct_url(
        stream_url,
        request,
        start=start,
        duration_seconds=track.duration_seconds,
        cache_key=_track_audio_cache_key(track.id, source_url),
    )


@router.post("/stream/track/{track_id}/prepare")
async def prepare_track(
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, int | bool | str]:
    """Prepare the seekable MP3 cache without transferring the audio body."""
    if getattr(request.state, "user_id", None) is None:
        raise HTTPException(status_code=401, detail="Missing auth user")

    track = get_track(db, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    source_url = await _resolve_track_source(track, db)
    cache_key = _track_audio_cache_key(track.id, source_url)
    cache_path = _audio_cache_path(cache_key)
    cache_hit = _cached_audio_is_valid(cache_path)
    if cache_hit:
        with contextlib.suppress(OSError):
            os.utime(cache_path, None)
    else:
        source_url, stream_url = await _resolve_track_stream(track, db, source_url)
        cache_key = _track_audio_cache_key(track.id, source_url)
        cache_path = await _ensure_cached_mp3(stream_url, cache_key)

    return {
        "track_id": track.id,
        "status": "ready",
        "cache_hit": cache_hit,
        "size_bytes": cache_path.stat().st_size,
    }


@router.get("/stream/proxy")
async def stream_proxy(
    request: Request,
    segment_url: str | None = Query(None, min_length=1),
    url: str | None = Query(None, min_length=1),
    start: float = Query(0.0, ge=0),
) -> FileResponse:
    stream_url = segment_url or url
    if not stream_url:
        raise HTTPException(status_code=422, detail="segment_url is required")
    _log(f"[STREAM PROXY] Local segment endpoint hit: {stream_url}")
    return await _stream_direct_url(stream_url, request, start=start, cache_key=f"proxy:{stream_url}")
