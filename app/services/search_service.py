import re
import threading

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
from app.services.normalization_service import normalize_name
from app.services.serialization_service import track_to_read


SEARCH_RESULT_LIMIT = 50
MIN_PROVIDER_RESULTS = 30
VARIANT_QUOTA = 2
_hydration_lock = threading.Lock()
_hydrating_queries: set[str] = set()

VARIANT_PATTERNS = {
    "slowed": re.compile(r"\bslowed\b|\bslow\s*\+\s*reverb\b|\bslowed\s*\+\s*reverb\b", re.IGNORECASE),
    "reverb": re.compile(r"\breverb\b", re.IGNORECASE),
    "speed": re.compile(r"\bspeed\s*up\b|\bspeedup\b|\bsped\s*up\b|\bspedup\b|\bnightcore\b", re.IGNORECASE),
}


def _log(message: str) -> None:
    print(message, flush=True)

SEARCH_PROVIDERS = (
    {
        "name": "soundcloud",
        "search": "scsearch50",
        "tag": "soundcloud",
        "default_genre": "soundcloud",
        "popularity_score": 75.0,
        "max_results": 50,
    },
    {
        "name": "youtube",
        "search": "ytsearch30",
        "tag": "youtube",
        "default_genre": "youtube",
        "popularity_score": 65.0,
        "max_results": 30,
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


def _variant_counter_from_tracks(results: list) -> dict[str, int]:
    counter = {name: 0 for name in VARIANT_PATTERNS}
    for track in results:
        for variant_type in _variant_types(track.title):
            counter[variant_type] += 1
    return counter


def _can_take_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> bool:
    return not variant_types or all(counter.get(variant_type, 0) < VARIANT_QUOTA for variant_type in variant_types)


def _count_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> None:
    for variant_type in variant_types:
        counter[variant_type] = counter.get(variant_type, 0) + 1


def _apply_variant_quota(results: list, limit: int = SEARCH_RESULT_LIMIT) -> list:
    counter = {name: 0 for name in VARIANT_PATTERNS}
    filtered = []
    for track in results:
        variant_types = _variant_types(track.title)
        if not _can_take_variant(counter, variant_types):
            continue
        _count_variant(counter, variant_types)
        filtered.append(track)
        if len(filtered) >= limit:
            break
    return filtered


def _is_known_provider_source(source_name: str | None, source_url: str | None) -> bool:
    source = (source_url or "").lower()
    return (
        (source_name or "").lower() in {"soundcloud", "youtube", "sc", "yt"}
        or "soundcloud.com" in source
        or "youtube.com" in source
        or "youtu.be" in source
    )


def _score_provider_result(query: str, entry: dict) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "creator", "description", "webpage_url")
    ).lower()
    tokens = [token for token in normalize_name(query).split() if len(token) > 1]
    return sum(1 for token in tokens if token in haystack)


def _search_provider(query: str, provider: dict) -> list[dict]:
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
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"{provider['search']}:{query}", download=False)

    entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
    playable = [entry for entry in entries if entry.get("webpage_url") or entry.get("url")]
    seen = set()
    unique = []
    for entry in playable:
        key = str(entry.get("id") or entry.get("webpage_url") or entry.get("url"))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    unique.sort(key=lambda entry: _score_provider_result(query, entry), reverse=True)
    return unique[: int(provider["max_results"])]


def _entry_artist_name(entry: dict, fallback: str) -> str:
    return (
        entry.get("uploader")
        or entry.get("artist")
        or entry.get("creator")
        or entry.get("channel")
        or fallback
    ).strip()


def _entry_artist_url(entry: dict) -> str | None:
    return entry.get("uploader_url") or entry.get("channel_url") or entry.get("creator_url")


def _save_provider_entry(db: Session, query: str, provider: dict, result: dict) -> bool:
    title = (result.get("title") or query).strip()
    artist_name = _entry_artist_name(result, query)
    source_url = result.get("webpage_url") or result.get("original_url") or result.get("url")
    if not title or not artist_name or not source_url:
        return False

    provider_name = str(provider["name"])
    external_id = str(result.get("id") or source_url)
    existing = find_track_by_provider_external_id(db, provider=provider_name, external_id=external_id)
    if existing:
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
        tags=["provider", str(provider["tag"])],
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


def _save_provider_tracks(db: Session, query: str, provider: dict, variant_counter: dict[str, int]) -> int:
    stored = 0
    for entry in _search_provider(query, provider):
        variant_types = _variant_types(" ".join(str(entry.get(key) or "") for key in ("title", "webpage_url", "url")))
        if not _can_take_variant(variant_counter, variant_types):
            continue
        try:
            if _save_provider_entry(db, query, provider, entry):
                stored += 1
                _count_variant(variant_counter, variant_types)
        except Exception:
            db.rollback()
    return stored


def _save_external_tracks(db: Session, query: str, existing_results: list | None = None) -> None:
    variant_counter = _variant_counter_from_tracks(existing_results or [])
    for provider in SEARCH_PROVIDERS:
        try:
            _log(f"[SEARCH HYDRATE] provider={provider['name']} start query={query}")
            stored = _save_provider_tracks(db, query, provider, variant_counter)
        except Exception:
            db.rollback()
            stored = 0
        _log(f"[SEARCH HYDRATE] provider={provider['name']} stored={stored} query={query}")
        if stored:
            hydrated_results = _apply_variant_quota(search_tracks(db, query, limit=SEARCH_RESULT_LIMIT))
            if _playable_provider_count(hydrated_results) >= SEARCH_RESULT_LIMIT:
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
            local_results = _apply_variant_quota(search_tracks(db, query, limit=SEARCH_RESULT_LIMIT))
            if _playable_provider_count(local_results) >= SEARCH_RESULT_LIMIT:
                return
            _log(f"[SEARCH HYDRATE] start query={query} local={len(local_results)}")
            _save_external_tracks(db, query, existing_results=local_results)
    finally:
        with _hydration_lock:
            _hydrating_queries.discard(normalized_query)


def _schedule_hydration(query: str, background_tasks: BackgroundTasks | None) -> None:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return
    with _hydration_lock:
        if normalized_query in _hydrating_queries:
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
) -> list[TrackRead]:
    local_results = _apply_variant_quota(search_tracks(db, query, limit=SEARCH_RESULT_LIMIT))
    if _playable_provider_count(local_results) < SEARCH_RESULT_LIMIT:
        _schedule_hydration(query, background_tasks)
    return [track_to_read(track) for track in local_results]
