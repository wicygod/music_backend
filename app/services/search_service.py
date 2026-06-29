import json
import random
import re
import time
import threading
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from fastapi import BackgroundTasks
import yt_dlp
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.repositories.artists import find_or_create_artist
from app.repositories.tracks import search_tracks
from app.repositories.tracks import (
    create_track_with_artist,
    find_duplicate_track_for_artist,
    find_track_by_provider_external_id,
)
from app.schemas.track import TrackRead
from app.schemas.track import TrackSeedCreate
from app.services.artist_cleanup_service import clean_provider_artist, provider_authority_score, title_without_artist_prefix
from app.services.normalization_service import normalize_name
from app.services.serialization_service import track_to_read
from app.services.proxy_rotator import proxy_rotator
from app.services.track_filter_service import dedupe_tracks, is_music_track


SEARCH_RESULT_LIMIT = 150
EXTERNAL_PARSE_LIMIT = 50
HYDRATION_COOLDOWN_SECONDS = 10 * 60
VARIANT_QUOTA = 2
DEDUP_SIMILARITY_THRESHOLD = 0.88
DEDUP_CATEGORY_QUOTAS = {
    "original": 2,
    "speed": 2,
    "slowed": 2,
    "custom": 2,
}
MAX_MUSIC_DURATION_SECONDS = 15 * 60
COOKIES_FILE = Path("secrets/cookies.txt")
_hydration_lock = threading.Lock()
_hydrating_queries: set[str] = set()
_hydrated_queries: dict[str, float] = {}

VARIANT_PATTERNS = {
    "slowed": re.compile(r"\bslowed\b|\bslow\s*\+\s*reverb\b|\bslowed\s*\+\s*reverb\b", re.IGNORECASE),
    "reverb": re.compile(r"\breverb\b", re.IGNORECASE),
    "speed": re.compile(r"\bspeed\s*up\b|\bspeedup\b|\bsped\s*up\b|\bspedup\b|\bnightcore\b", re.IGNORECASE),
}
CUSTOM_VARIANT_RE = re.compile(
    r"\b("
    r"breakcore|bass\s*boost(?:ed)?|instrumental|karaoke|remix|deep\s*remix|edit|bootleg|mashup|"
    r"cover|ai|8d|lofi|acoustic|live|version|visualizer|sped\s*down|pitch(?:ed)?"
    r"|кавер|ремикс|инструментал"
    r")\b",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s]*)?", re.IGNORECASE)
FILE_EXT_RE = re.compile(r"\.(?:mp3|m4a|webm|wav|flac|aac|ogg)\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
NON_TITLE_CHARS_RE = re.compile(r"[^\w\s]+", re.UNICODE)
SPACES_RE = re.compile(r"\s+")
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


def _log(message: str) -> None:
    print(message, flush=True)


def _yt_dlp_options(**overrides) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 10,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
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
        return (parse_qs(parsed.query).get("v") or [None])[0]
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None
    return None


def _canonical_youtube_music_url(source_url: str | None, external_id: str | None = None) -> str | None:
    video_id = _youtube_video_id(source_url) or (external_id if external_id and not external_id.isdigit() else None)
    if not video_id:
        return None
    return f"https://music.youtube.com/watch?v={video_id}"


def _is_youtube_music_source(source_url: str | None) -> bool:
    return _youtube_video_id(source_url) is not None


def _candidate_source_url(provider_name: str, entry: dict) -> str | None:
    source_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if provider_name == "youtube":
        if source_url and not _youtube_video_id(str(source_url)):
            return None
        return _canonical_youtube_music_url(str(source_url or ""), str(entry.get("id") or ""))
    if provider_name == "soundcloud" and _is_soundcloud_source(source_url):
        return str(source_url)
    return None


def _is_allowed_provider_entry(provider_name: str, entry: dict) -> bool:
    source_url = _candidate_source_url(provider_name, entry)
    if not source_url:
        return False

    duration_seconds = int(entry.get("duration") or 0)
    if duration_seconds and duration_seconds > MAX_MUSIC_DURATION_SECONDS:
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
        categories = " ".join(str(item) for item in (entry.get("categories") or []))
        channel = str(entry.get("channel") or entry.get("uploader") or "")
        title = str(entry.get("title") or "")
        return (
            _is_youtube_music_source(source_url)
            and (
                "music" in categories.lower()
                or "music.youtube.com" in str(entry.get("webpage_url") or entry.get("url") or "").lower()
                or channel.lower().endswith(" - topic")
                or "official audio" in title.lower()
                or "official music video" in title.lower()
            )
        )

    return False

SEARCH_PROVIDERS = (
    {
        "name": "soundcloud",
        "search_prefix": "scsearch",
        "tag": "soundcloud",
        "default_genre": "soundcloud",
        "popularity_score": 75.0,
    },
    {
        "name": "youtube",
        "search_prefix": None,
        "search_url": "https://music.youtube.com/search?q={query}",
        "tag": "youtube_music",
        "default_genre": "youtube_music",
        "popularity_score": 65.0,
    },
)


def _playable_provider_count(results: list) -> int:
    return sum(
        bool(track.is_playable)
        and _is_known_provider_source(track.source_name, track.source_url)
        for track in results
    )


def _variant_types(text: str | None) -> tuple[str, ...]:
    haystack = (text or "").lower()
    return tuple(name for name, pattern in VARIANT_PATTERNS.items() if pattern.search(haystack))


def _dedupe_category(text: str | None) -> str:
    haystack = (text or "").lower()
    variant_types = set(_variant_types(haystack))
    if variant_types & {"speed"}:
        return "speed"
    if variant_types & {"slowed", "reverb"}:
        return "slowed"
    if CUSTOM_VARIANT_RE.search(haystack):
        return "custom"
    return "original"


def _clean_title_for_grouping(title: str | None) -> str:
    cleaned = (title or "").lower()
    cleaned = DOMAIN_RE.sub(" ", cleaned)
    cleaned = FILE_EXT_RE.sub(" ", cleaned)
    cleaned = BRACKET_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:official\s+)?(?:audio|video|music\s+video|lyrics?|lyric\s+video)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:speed\s*up|speedup|sped\s*up|spedup|nightcore|slowed|reverb|slow\s*\+\s*reverb)\b", " ", cleaned)
    cleaned = CUSTOM_VARIANT_RE.sub(" ", cleaned)
    cleaned = NON_TITLE_CHARS_RE.sub(" ", cleaned)
    cleaned = SPACES_RE.sub(" ", cleaned).strip()
    return cleaned or "unknown"


def _title_from_item(item) -> str:
    if isinstance(item, dict):
        return str(item.get("title") or "")
    return str(getattr(item, "title", "") or "")


def _title_tokens(value: str) -> set[str]:
    return {token for token in value.split() if len(token) >= 3}


def _looks_like_same_song(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 3 and (left in right or right in left):
        return True
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if left_tokens and right_tokens:
        if left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens):
            return True
        jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if jaccard >= 0.85:
            return True
    return SequenceMatcher(None, left, right).ratio() >= DEDUP_SIMILARITY_THRESHOLD


def _title_spam_penalty(title: str) -> int:
    haystack = (title or "").lower()
    penalty = 0
    if DOMAIN_RE.search(haystack):
        penalty += 2
    if FILE_EXT_RE.search(haystack):
        penalty += 2
    if "spotidown" in haystack or "lightaudio" in haystack:
        penalty += 3
    return penalty


def _empty_dedupe_counts() -> dict[str, int]:
    return {name: 0 for name in DEDUP_CATEGORY_QUOTAS}


def _take_with_song_dedupe(groups: list[dict], item) -> bool:
    title = _title_from_item(item)
    base_title = _clean_title_for_grouping(title)
    category = _dedupe_category(title)
    group = next((candidate for candidate in groups if _looks_like_same_song(base_title, candidate["base"])), None)
    if not group:
        group = {"base": base_title, "counts": _empty_dedupe_counts()}
        groups.append(group)
    if group["counts"].get(category, 0) >= DEDUP_CATEGORY_QUOTAS[category]:
        return False
    group["counts"][category] = group["counts"].get(category, 0) + 1
    return True


def _dedupe_groups_from_items(items: list) -> list[dict]:
    groups: list[dict] = []
    for item in items:
        _take_with_song_dedupe(groups, item)
    return groups


def _apply_song_dedupe_quota(items: list, limit: int = SEARCH_RESULT_LIMIT) -> list:
    groups: list[dict] = []
    filtered = []
    ordered_items = sorted(enumerate(items), key=lambda pair: (_title_spam_penalty(_title_from_item(pair[1])), pair[0]))
    for _index, item in ordered_items:
        if not _take_with_song_dedupe(groups, item):
            continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def _variant_counter_from_tracks(results: list) -> dict[str, int]:
    counter = {name: 0 for name in VARIANT_PATTERNS}
    counter["_total"] = 0
    for track in results:
        variant_types = _variant_types(track.title)
        if not variant_types:
            continue
        counter["_total"] += 1
        for variant_type in variant_types:
            counter[variant_type] += 1
    return counter


def _can_take_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> bool:
    return not variant_types or counter.get("_total", 0) < VARIANT_QUOTA


def _count_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> None:
    if not variant_types:
        return
    counter["_total"] = counter.get("_total", 0) + 1
    for variant_type in variant_types:
        counter[variant_type] = counter.get(variant_type, 0) + 1


def _apply_variant_quota(results: list, limit: int = SEARCH_RESULT_LIMIT) -> list:
    return _apply_song_dedupe_quota(results, limit=limit)


def _is_clean_catalog_track(track) -> bool:
    if not is_music_track(track):
        return False
    if BAD_VIDEO_TERMS_RE.search(" ".join(str(value or "") for value in (track.title, track.source_url))):
        return False
    if not track.source_url:
        return True
    if track.is_playable:
        return _is_known_provider_source(track.source_name, track.source_url)
    return not any(bad in str(track.source_url).lower() for bad in ("vk.com", "vkontakte", "dzen.ru", "rutube.ru"))


def _catalog_track_matches_query(track, query: str) -> bool:
    tokens = _query_tokens(query)
    if len(tokens) <= 1:
        return True
    artists = " ".join(
        link.artist.name
        for link in getattr(track, "artist_links", []) or []
        if getattr(link, "artist", None) and link.artist.name
    )
    title_haystack = normalize_name(track.title)
    artist_haystack = normalize_name(artists)
    if all(token in title_haystack for token in tokens):
        return True
    if all(token in artist_haystack for token in tokens):
        return True
    return bool(tokens[0] in artist_haystack and all(token in title_haystack for token in tokens[1:]))


def _query_tokens(query: str) -> list[str]:
    return [token for token in normalize_name(query).split() if len(token) > 1]


def _title_matches_query(title: str | None, query: str) -> bool:
    tokens = _query_tokens(query)
    if len(tokens) <= 1:
        return True
    haystack = normalize_name(title or "")
    return all(token in haystack for token in tokens)


def _prefer_title_matches(items: list, query: str) -> list:
    if not items:
        return items
    title_matches = [item for item in items if _title_matches_query(_title_from_item(item), query)]
    return title_matches or items


def _canonicalize_catalog_track_source(track) -> bool:
    name = (track.source_name or "").lower()
    if name not in {"youtube", "youtube_music", "yt"}:
        return False
    canonical_url = _canonical_youtube_music_url(track.source_url, track.source_external_id)
    if not canonical_url:
        return False
    changed = False
    if track.source_url != canonical_url:
        track.source_url = canonical_url
        changed = True
    if name != "youtube":
        track.source_name = "youtube"
        changed = True
    return changed


def _is_known_provider_source(source_name: str | None, source_url: str | None) -> bool:
    name = (source_name or "").lower()
    if name in {"soundcloud", "sc"}:
        return _is_soundcloud_source(source_url)
    if name in {"youtube", "youtube_music", "yt"}:
        return _is_youtube_music_source(source_url)
    return _is_soundcloud_source(source_url) or _is_youtube_music_source(source_url)


def _score_provider_result(query: str, entry: dict) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "creator", "description", "webpage_url")
    ).lower()
    tokens = [token for token in normalize_name(query).split() if len(token) > 1]
    return sum(1 for token in tokens if token in haystack)


def _search_provider(query: str, provider: dict, limit: int) -> list[dict]:
    options = _yt_dlp_options(extract_flat=True)
    search_limit = max(1, min(EXTERNAL_PARSE_LIMIT, limit))
    search_url = (
        f"{provider['search_prefix']}{search_limit}:{query}"
        if provider.get("search_prefix")
        else str(provider["search_url"]).format(query=quote_plus(query))
    )
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(search_url, download=False)

    entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
    provider_name = str(provider["name"])
    playable = [entry for entry in entries if _is_allowed_provider_entry(provider_name, entry)]
    seen = set()
    unique = []
    for entry in playable:
        key = str(entry.get("id") or _candidate_source_url(provider_name, entry))
        if not key or key in seen:
            continue
        seen.add(key)
        entry["_search_order"] = len(unique)
        unique.append(entry)
    if provider_name == "youtube":
        unique.sort(
            key=lambda entry: (
                provider_authority_score(entry.get("title"), _entry_artist_name(entry, ""), query),
                -int(entry.get("_search_order") or 0),
            ),
            reverse=True,
        )
    else:
        unique.sort(
            key=lambda entry: (
                provider_authority_score(entry.get("title"), _entry_artist_name(entry, ""), query),
                _score_provider_result(query, entry),
            ),
            reverse=True,
        )
    return unique[:search_limit]


def _entry_artist_name(entry: dict, fallback: str) -> str:
    return str(
        entry.get("uploader")
        or entry.get("artist")
        or entry.get("creator")
        or entry.get("channel")
        or fallback
    ).strip()


def _entry_artist_url(entry: dict) -> str | None:
    return entry.get("uploader_url") or entry.get("channel_url") or entry.get("creator_url")


def _query_tag(query: str) -> str | None:
    normalized = normalize_name(query)
    return normalized or None


def _ensure_query_tag(track, query: str) -> bool:
    tag = _query_tag(query)
    if not tag:
        return False
    try:
        tags = json.loads(track.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    string_tags = [str(item) for item in tags]
    if tag in string_tags:
        return False
    string_tags.append(tag)
    track.tags_json = json.dumps(string_tags)
    return True


def _save_provider_entry(db: Session, query: str, provider: dict, result: dict) -> bool:
    raw_title = (result.get("title") or query).strip()
    raw_artist_name = _entry_artist_name(result, query)
    title = title_without_artist_prefix(raw_title)
    artist_name = clean_provider_artist(raw_title, raw_artist_name, query)
    provider_name = str(provider["name"])
    source_url = _candidate_source_url(provider_name, result)
    if not title or not artist_name or not source_url or not _is_allowed_provider_entry(provider_name, result):
        return False

    external_id = str(result.get("id") or source_url)
    existing = find_track_by_provider_external_id(db, provider=provider_name, external_id=external_id)
    if existing:
        changed = _canonicalize_catalog_track_source(existing)
        if _ensure_query_tag(existing, query):
            changed = True
        if changed:
            db.add(existing)
            db.commit()
        return True

    duration_seconds = int(result.get("duration") or 0)
    normalized_artist = normalize_name(artist_name)
    duplicate = find_duplicate_track_for_artist(
        db,
        normalized_artist=normalized_artist,
        title=title,
        duration_seconds=duration_seconds,
    )
    if duplicate:
        if not duplicate.is_playable or not _is_known_provider_source(duplicate.source_name, duplicate.source_url):
            duplicate.is_playable = True
            duplicate.source_name = provider_name
            duplicate.source_external_id = external_id
            duplicate.source_url = str(source_url)
            duplicate.cover_url = duplicate.cover_url or result.get("thumbnail")
            duplicate.genre = duplicate.genre or result.get("genre") or provider["default_genre"]
            _ensure_query_tag(duplicate, query)
            db.add(duplicate)
            db.commit()
        else:
            changed = _canonicalize_catalog_track_source(duplicate)
            if _ensure_query_tag(duplicate, query):
                changed = True
            if changed:
                db.add(duplicate)
                db.commit()
        return True

    artist_url = _entry_artist_url(result)
    artist, _created = find_or_create_artist(
        db,
        name=artist_name,
        region="global",
        avatar_url=artist_url,
        genres=[str(provider["tag"])],
        source_name=provider_name,
        source_external_id=str(result.get("uploader_id") or result.get("channel_id") or artist_name),
        source_url=artist_url,
        import_status="imported",
    )
    payload = TrackSeedCreate(
        title=title,
        artist=artist_name,
        duration_seconds=duration_seconds,
        cover_url=result.get("thumbnail"),
        genre=result.get("genre") or provider["default_genre"],
        tags=["provider", str(provider["tag"]), *([_query_tag(query)] if _query_tag(query) else [])],
        region="global",
        popularity_score=float(provider["popularity_score"]),
        quality_score=100.0,
        is_playable=True,
        audio_src=None,
        source_name=provider_name,
        source_external_id=external_id,
        source_url=str(source_url),
        needs_review=False,
    )
    create_track_with_artist(db, payload, artist)
    db.commit()
    return True


def _save_provider_tracks(
    db: Session,
    query: str,
    provider: dict,
    dedupe_groups: list[dict],
    remaining: int,
) -> int:
    accepted = 0
    provider_entries = _prefer_title_matches(_search_provider(query, provider, limit=EXTERNAL_PARSE_LIMIT), query)
    for entry in provider_entries:
        if accepted >= remaining:
            break
        if not _take_with_song_dedupe(dedupe_groups, entry):
            continue
        try:
            if _save_provider_entry(db, query, provider, entry):
                accepted += 1
        except Exception:
            db.rollback()
    return accepted


def _save_external_tracks(db: Session, query: str, existing_results: list | None = None) -> None:
    dedupe_groups = _dedupe_groups_from_items(existing_results or [])
    accepted_total = 0
    for provider in SEARCH_PROVIDERS:
        remaining = EXTERNAL_PARSE_LIMIT - accepted_total
        if remaining <= 0:
            return
        try:
            _log(f"[SEARCH HYDRATE] provider={provider['name']} start query={query}")
            stored = _save_provider_tracks(db, query, provider, dedupe_groups, remaining)
        except Exception:
            db.rollback()
            stored = 0
        accepted_total += stored
        _log(f"[SEARCH HYDRATE] provider={provider['name']} stored={stored} query={query}")
        if accepted_total >= EXTERNAL_PARSE_LIMIT:
            return


def hydrate_search_catalog(query: str) -> None:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return
    with _hydration_lock:
        if normalized_query in _hydrating_queries:
            return
        _hydrating_queries.add(normalized_query)

    try:
        with SessionLocal() as db:
            candidates = [
                track
                for track in search_tracks(db, query, limit=SEARCH_RESULT_LIMIT * 2)
                if _is_clean_catalog_track(track) and _catalog_track_matches_query(track, query)
            ]
            candidates = _prefer_title_matches(candidates, query)
            local_results = _apply_variant_quota(
                candidates
            )
            _log(f"[SEARCH HYDRATE] start query={query} local={len(local_results)}")
            _save_external_tracks(db, query, existing_results=local_results)
    finally:
        with _hydration_lock:
            _hydrating_queries.discard(normalized_query)
            _hydrated_queries[normalized_query] = time.monotonic()


def _schedule_hydration(query: str, background_tasks: BackgroundTasks | None) -> None:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return
    with _hydration_lock:
        if normalized_query in _hydrating_queries:
            return
        hydrated_at = _hydrated_queries.get(normalized_query)
        if hydrated_at and time.monotonic() - hydrated_at < HYDRATION_COOLDOWN_SECONDS:
            return
    if background_tasks:
        background_tasks.add_task(hydrate_search_catalog, query)
    else:
        thread = threading.Thread(target=hydrate_search_catalog, args=(query,), daemon=True)
        thread.start()


def search_local_catalog(
    db: Session,
    query: str,
    background_tasks: BackgroundTasks | None = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> list[TrackRead]:
    safe_limit = max(1, min(limit, SEARCH_RESULT_LIMIT))
    candidate_limit = min(SEARCH_RESULT_LIMIT * 2, max(safe_limit * 2, safe_limit + 20))
    candidates = [
        track
        for track in search_tracks(db, query, limit=candidate_limit)
        if _is_clean_catalog_track(track) and _catalog_track_matches_query(track, query)
    ]
    candidates = _prefer_title_matches(candidates, query)
    local_results = _apply_variant_quota(
        candidates,
        limit=safe_limit,
    )
    local_results = dedupe_tracks(local_results, limit=safe_limit)
    changed = False
    for track in local_results:
        if _canonicalize_catalog_track_source(track):
            changed = True
    if changed:
        for track in local_results:
            db.add(track)
        db.commit()
    _schedule_hydration(query, background_tasks)
    return [track_to_read(track) for track in local_results]
