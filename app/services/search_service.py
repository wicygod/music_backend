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


def _has_soundcloud_track(results: list) -> bool:
    return any(
        (track.source_name or "").lower() == "soundcloud"
        or "soundcloud.com" in (track.source_url or "").lower()
        for track in results
    )


def _score_soundcloud_result(query: str, entry: dict) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "description", "webpage_url")
    ).lower()
    tokens = [token for token in normalize_name(query).split() if len(token) > 1]
    return sum(1 for token in tokens if token in haystack)


def _search_soundcloud(query: str) -> dict | None:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"scsearch5:{query}", download=False)

    entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
    playable = [entry for entry in entries if entry.get("webpage_url") or entry.get("url")]
    if not playable:
        return None
    return max(playable, key=lambda entry: _score_soundcloud_result(query, entry))


def _save_soundcloud_track(db: Session, query: str) -> None:
    try:
        result = _search_soundcloud(query)
        if not result:
            return

        title = (result.get("title") or query).strip()
        artist_name = (result.get("uploader") or result.get("artist") or query).strip()
        source_url = result.get("webpage_url") or result.get("original_url") or result.get("url")
        if not title or not artist_name or not source_url:
            return

        external_id = str(result.get("id") or source_url)
        existing = find_track_by_provider_external_id(db, provider="soundcloud", external_id=external_id)
        if existing:
            return

        normalized_artist = normalize_name(artist_name)
        duplicate = find_duplicate_track_for_artist(
            db,
            normalized_artist=normalized_artist,
            title=title,
            duration_seconds=int(result.get("duration") or 0),
        )
        if duplicate and "soundcloud.com" in (duplicate.source_url or "").lower():
            return

        artist, _created = find_or_create_artist(
            db,
            name=artist_name,
            region="global",
            avatar_url=result.get("uploader_url"),
            genres=["soundcloud"],
            source_name="soundcloud",
            source_external_id=str(result.get("uploader_id") or artist_name),
            source_url=result.get("uploader_url"),
            import_status="imported",
        )
        payload = TrackSeedCreate(
            title=title,
            artist=artist_name,
            duration_seconds=int(result.get("duration") or 0),
            cover_url=result.get("thumbnail"),
            genre=result.get("genre") or "soundcloud",
            tags=["provider", "soundcloud"],
            region="global",
            popularity_score=75.0,
            quality_score=100.0,
            is_playable=True,
            audio_src=None,
            source_name="soundcloud",
            source_external_id=external_id,
            source_url=str(source_url),
            needs_review=False,
        )
        create_track_with_artist(db, payload, artist)
        db.commit()
    except Exception:
        db.rollback()


def search_local_catalog(db: Session, query: str) -> list[TrackRead]:
    local_results = search_tracks(db, query)
    if not _has_soundcloud_track(local_results):
        _save_soundcloud_track(db, query)
        local_results = search_tracks(db, query)
    return [track_to_read(track) for track in local_results]
