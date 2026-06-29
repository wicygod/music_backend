import json
from typing import Any

from app.models.artist import Artist
from app.models.import_job import ImportJob
from app.models.playlist import UserFavorite, UserPlaylist
from app.models.track import Track
from app.schemas.artist import ArtistRead, ArtistSummary, ArtistWithTracks
from app.schemas.import_job import ImportJobRead
from app.schemas.playlist import FavoriteRead, PlaylistRead
from app.schemas.track import TrackRead
from app.services.artist_cleanup_service import artist_from_title
from app.services.normalization_service import normalize_name


POPULAR_DISPLAY_ARTISTS = {
    "lil peep": "Lil Peep",
    "9 mice": "9 mice",
    "kai angel": "Kai Angel",
    "viperr": "Viperr",
    "pharaoh": "Pharaoh",
    "\u0442\u0451\u043c\u043d\u044b\u0439 \u043f\u0440\u0438\u043d\u0446": "\u0422\u0451\u043c\u043d\u044b\u0439 \u041f\u0440\u0438\u043d\u0446",
    "fortuna812": "fortuna812",
    "face": "Face",
    "cupsize": "CUPSIZE",
    "madkid": "madkid",
    "\u0441\u043d\u044f\u043b\u0446\u0435\u043f\u0438": "\u0441\u043d\u044f\u043b\u0446\u0435\u043f\u0438",
}


def parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def artist_summary(artist: Artist) -> ArtistSummary:
    return ArtistSummary(
        id=artist.id,
        name=artist.name,
        avatar_url=artist.avatar_url,
        region=artist.region,
    )


def artist_to_read(artist: Artist, track_count: int | None = None) -> ArtistRead | ArtistWithTracks:
    payload = {
        "id": artist.id,
        "name": artist.name,
        "normalized_name": artist.normalized_name,
        "avatar_url": artist.avatar_url,
        "region": artist.region,
        "genres": parse_json_list(artist.genres_json),
        "popularity_score": artist.popularity_score,
        "source_name": artist.source_name,
        "source_external_id": artist.source_external_id,
        "source_url": artist.source_url,
        "confidence_score": artist.confidence_score,
        "needs_review": artist.needs_review,
        "priority": artist.priority,
        "tracks_target": artist.tracks_target,
        "seed_source": artist.seed_source,
        "import_status": artist.import_status,
        "last_imported_at": artist.last_imported_at,
        "created_at": artist.created_at,
        "updated_at": artist.updated_at,
    }
    if track_count is None:
        return ArtistRead(**payload)
    return ArtistWithTracks(**payload, track_count=track_count)


def track_to_read(track: Track) -> TrackRead:
    main_first = sorted(track.artist_links, key=lambda link: 0 if link.role == "main" else 1)
    artists = [artist_summary(link.artist) for link in main_first]
    parsed_artist = _popular_artist_from_title(track.title) or artist_from_title(track.title)
    if parsed_artist and artists and normalize_name(artists[0].name) != normalize_name(parsed_artist):
        artists[0] = artists[0].model_copy(update={"name": parsed_artist})
    return TrackRead(
        id=track.id,
        title=track.title,
        normalized_title=track.normalized_title,
        duration_seconds=track.duration_seconds,
        cover_url=track.cover_url,
        genre=track.genre,
        tags=parse_json_list(track.tags_json),
        language=track.language,
        region=track.region,
        popularity_score=track.popularity_score,
        quality_score=track.quality_score,
        is_playable=track.is_playable,
        audio_src=track.audio_src,
        source_name=track.source_name,
        source_external_id=track.source_external_id,
        source_url=track.source_url,
        needs_review=track.needs_review,
        artists=artists,
        created_at=track.created_at,
        updated_at=track.updated_at,
    )


def _popular_artist_from_title(title: str | None) -> str | None:
    normalized = normalize_name(title or "")
    for key, display_name in POPULAR_DISPLAY_ARTISTS.items():
        if key and (normalized == key or normalized.startswith(f"{key} ") or f" {key} " in f" {normalized} "):
            return display_name
    return None


def playlist_to_read(playlist: UserPlaylist) -> PlaylistRead:
    tracks = sorted(playlist.track_links, key=lambda link: link.added_at)
    return PlaylistRead(
        id=playlist.id,
        user_id=playlist.user_id,
        name=playlist.name,
        description=playlist.description,
        created_at=playlist.created_at,
        updated_at=playlist.updated_at,
        tracks=[track_to_read(link.track) for link in tracks],
    )


def favorite_to_read(favorite: UserFavorite) -> FavoriteRead:
    return FavoriteRead(
        user_id=favorite.user_id,
        track=track_to_read(favorite.track),
        created_at=favorite.created_at,
    )


def import_job_to_read(job: ImportJob) -> ImportJobRead:
    return ImportJobRead(
        id=job.id,
        type=job.type,
        payload=parse_json_object(job.payload_json),
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
