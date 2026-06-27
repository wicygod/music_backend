import yt_dlp
from sqlalchemy.orm import Session

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


SEARCH_PROVIDERS = (
    {
        "name": "soundcloud",
        "search": "scsearch5",
        "tag": "soundcloud",
        "default_genre": "soundcloud",
        "popularity_score": 75.0,
    },
    {
        "name": "youtube",
        "search": "ytsearch5",
        "tag": "youtube",
        "default_genre": "youtube",
        "popularity_score": 65.0,
    },
)


def _has_playable_provider_track(results: list) -> bool:
    return any(
        bool(track.is_playable)
        and _is_known_provider_source(track.source_name, track.source_url)
        for track in results
    )


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


def _search_provider(query: str, provider: dict) -> dict | None:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"{provider['search']}:{query}", download=False)

    entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
    playable = [entry for entry in entries if entry.get("webpage_url") or entry.get("url")]
    if not playable:
        return None
    return max(playable, key=lambda entry: _score_provider_result(query, entry))


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


def _save_provider_track(db: Session, query: str, provider: dict) -> bool:
    result = _search_provider(query, provider)
    if not result:
        return False

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


def _save_external_track(db: Session, query: str) -> None:
    try:
        for provider in SEARCH_PROVIDERS:
            if _save_provider_track(db, query, provider):
                return
    except Exception:
        db.rollback()


def search_local_catalog(db: Session, query: str) -> list[TrackRead]:
    local_results = search_tracks(db, query)
    if not _has_playable_provider_track(local_results):
        _save_external_track(db, query)
        local_results = search_tracks(db, query)
    return [track_to_read(track) for track in local_results]
