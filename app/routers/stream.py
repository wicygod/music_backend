import time
import re
from urllib.parse import quote, urljoin

import httpx
import yt_dlp
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.track import Track
from app.repositories.tracks import get_track


router = APIRouter(prefix="/api", tags=["stream"])

STREAM_CACHE_TTL_SECONDS = 15 * 60
_stream_cache: dict[str, tuple[float, str]] = {}
HLS_URI_RE = re.compile(r'URI="([^"]+)"')
UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}
SEARCH_PROVIDERS = ("scsearch5", "ytsearch5")
CORS_HEADERS = {
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


def _log(message: str) -> None:
    print(message, flush=True)


def _extract_stream_url(source_url: str) -> str:
    _log(f"[STREAM] Requested track URL: {source_url}")
    options = {
        "format": "bestaudio/best",
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
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


def _search_playable_source(query: str) -> tuple[str, str]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    errors: list[str] = []
    for provider in SEARCH_PROVIDERS:
        search_query = f"{provider}:{query}"
        _log(f"[STREAM SEARCH] Searching {provider}: {query}")
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(search_query, download=False)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            _log(f"[STREAM SEARCH ERROR] {provider} failed for {query}: {exc}")
            continue

        entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
        playable = [entry for entry in entries if entry.get("webpage_url") or entry.get("original_url") or entry.get("url")]
        if not playable:
            continue

        selected = max(playable, key=lambda entry: _score_search_entry(query, entry))
        source_url = selected.get("webpage_url") or selected.get("original_url") or selected.get("url")
        if source_url:
            title = selected.get("title") or "unknown"
            _log(f"[STREAM SEARCH] Selected {provider}: {title} -> {source_url}")
            return str(source_url), provider.split("search", 1)[0]

    detail = "; ".join(errors) if errors else "No playable search result"
    raise HTTPException(status_code=404, detail=f"Playable source not found: {detail}")


def _is_known_stream_source(source_url: str | None) -> bool:
    if not source_url:
        return False
    source = source_url.lower()
    return "soundcloud.com" in source or "youtu.be" in source or "youtube.com" in source


async def _resolve_track_source(track: Track, db: Session) -> str:
    if track.audio_src:
        return track.audio_src
    if _is_known_stream_source(track.source_url):
        return str(track.source_url)

    query = _track_query(track)
    if not query:
        raise HTTPException(status_code=404, detail="Track has no searchable title")

    source_url, provider = await run_in_threadpool(_search_playable_source, query)
    track.source_url = source_url
    track.source_name = provider
    track.is_playable = True
    db.add(track)
    db.commit()
    _log(f"[STREAM TRACK] Saved playable source for track_id={track.id}: {source_url}")
    return source_url


async def _get_cached_stream_url(source_url: str) -> str:
    now = time.monotonic()
    cached = _stream_cache.get(source_url)
    if cached and now - cached[0] < STREAM_CACHE_TTL_SECONDS:
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


async def proxy_stream(stream_url: str):
    _log(f"[STREAM PROXY] Segment/audio request: {stream_url}")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None, headers=UPSTREAM_HEADERS) as client:
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
    async with httpx.AsyncClient(follow_redirects=True, timeout=None, headers=UPSTREAM_HEADERS) as client:
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
    url = stream_url.lower().split("?", 1)[0]
    if url.endswith(".m3u8"):
        return "application/x-mpegURL"
    if url.endswith(".aac"):
        return "audio/aac"
    if url.endswith(".mp4") or url.endswith(".m4a") or url.endswith(".m4s") or url.endswith(".ts"):
        return "video/mp2t" if url.endswith(".ts") else "audio/mp4"
    return "audio/mpeg"


def _stream_direct_url(stream_url: str) -> StreamingResponse:
    media_type = _media_type_for(stream_url)
    _log(f"[STREAM] Serving via local proxy: media_type={media_type}, hls={'.m3u8' in stream_url.lower()}")
    if ".m3u8" in stream_url.lower():
        return StreamingResponse(proxy_hls_playlist(stream_url), media_type=media_type, headers=CORS_HEADERS)
    return StreamingResponse(proxy_stream(stream_url), media_type=media_type, headers=CORS_HEADERS)


@router.get("/stream")
async def stream(url: str = Query(..., min_length=1)) -> StreamingResponse:
    stream_url = await _get_cached_stream_url(url)
    return _stream_direct_url(stream_url)


@router.get("/stream/track/{track_id}")
async def stream_track(track_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    track = get_track(db, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    source_url = await _resolve_track_source(track, db)
    stream_url = await _get_cached_stream_url(source_url)
    return _stream_direct_url(stream_url)


@router.get("/stream/proxy")
async def stream_proxy(
    segment_url: str | None = Query(None, min_length=1),
    url: str | None = Query(None, min_length=1),
) -> StreamingResponse:
    stream_url = segment_url or url
    if not stream_url:
        raise HTTPException(status_code=422, detail="segment_url is required")
    _log(f"[STREAM PROXY] Local segment endpoint hit: {stream_url}")
    return _stream_direct_url(stream_url)
