import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.schemas.track import TrackSeedCreate
from app.services.normalization_service import normalize_name, normalize_title, normalize_track_title_for_dedupe


def with_artists(stmt):
    return stmt.options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))


def get_track(db: Session, track_id: int) -> Track | None:
    stmt = with_artists(select(Track).where(Track.id == track_id))
    return db.execute(stmt).scalars().unique().first()


def find_duplicate_track(db: Session, *, normalized_artist: str, normalized_title: str) -> Track | None:
    stmt = (
        with_artists(select(Track))
        .join(TrackArtist)
        .join(Artist)
        .where(
            Track.normalized_title == normalized_title,
            Artist.normalized_name == normalized_artist,
            TrackArtist.role == "main",
        )
    )
    return db.execute(stmt).scalars().unique().first()


def find_duplicate_track_for_artist(
    db: Session,
    *,
    normalized_artist: str,
    title: str,
    duration_seconds: int | None = None,
    duration_tolerance: int = 5,
) -> Track | None:
    canonical_title = normalize_track_title_for_dedupe(title)
    stmt = (
        with_artists(select(Track))
        .join(TrackArtist)
        .join(Artist)
        .where(Artist.normalized_name == normalized_artist)
    )
    candidates = db.execute(stmt).scalars().unique().all()
    for track in candidates:
        if normalize_track_title_for_dedupe(track.title) != canonical_title:
            continue
        if duration_seconds and track.duration_seconds:
            if abs(track.duration_seconds - duration_seconds) > duration_tolerance:
                continue
        return track
    return None


def find_track_by_provider_external_id(db: Session, *, provider: str, external_id: str) -> Track | None:
    if not provider or not external_id:
        return None
    stmt = with_artists(
        select(Track).where(
            Track.source_name == provider,
            Track.source_external_id == external_id,
        )
    )
    return db.execute(stmt).scalars().unique().first()


def filtered_feed_stmt():
    return select(Track).where(
        Track.quality_score >= 60,
        Track.needs_review == False,
    )


def ensure_track_artist_link(db: Session, *, track_id: int, artist_id: int, role: str = "main") -> None:
    existing = db.get(TrackArtist, {"track_id": track_id, "artist_id": artist_id, "role": role})
    if existing:
        return
    db.add(TrackArtist(track_id=track_id, artist_id=artist_id, role=role))
    db.flush()


def create_track_with_artist(db: Session, payload: TrackSeedCreate, artist: Artist) -> Track:
    track = Track(
        title=payload.title.strip(),
        normalized_title=normalize_title(payload.title),
        duration_seconds=payload.duration_seconds,
        cover_url=payload.cover_url,
        genre=payload.genre,
        tags_json=json.dumps(payload.tags),
        language=payload.language,
        region=payload.region,
        popularity_score=payload.popularity_score,
        quality_score=payload.quality_score,
        is_playable=payload.is_playable and bool(payload.audio_src or payload.source_url),
        audio_src=payload.audio_src if payload.is_playable else None,
        source_name=payload.source_name,
        source_external_id=payload.source_external_id,
        source_url=payload.source_url,
        needs_review=payload.needs_review,
    )
    db.add(track)
    db.flush()
    ensure_track_artist_link(db, track_id=track.id, artist_id=artist.id)
    return get_track(db, track.id) or track


def search_tracks(db: Session, query: str, limit: int = 25) -> list[Track]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []
    pattern = f"%{normalized_query}%"
    title_pattern = f"%{normalize_title(query)}%"
    stmt = (
        with_artists(select(Track))
        .join(TrackArtist)
        .join(Artist)
        .where(
            or_(
                Track.normalized_title.like(title_pattern),
                Artist.normalized_name.like(pattern),
                Track.genre.like(pattern),
            )
        )
        .order_by(Track.popularity_score.desc(), Track.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().unique().all())


def list_recent_tracks(db: Session, limit: int = 12) -> list[Track]:
    stmt = with_artists(filtered_feed_stmt().order_by(Track.created_at.desc()).limit(limit))
    return list(db.execute(stmt).scalars().unique().all())


def list_random_tracks(db: Session, limit: int = 12) -> list[Track]:
    stmt = with_artists(filtered_feed_stmt().order_by(func.random()).limit(limit))
    return list(db.execute(stmt).scalars().unique().all())


def list_trending_tracks(db: Session, limit: int = 12) -> list[Track]:
    stmt = with_artists(
        filtered_feed_stmt().order_by(Track.popularity_score.desc(), Track.quality_score.desc()).limit(limit)
    )
    return list(db.execute(stmt).scalars().unique().all())


def list_region_tracks(db: Session, region: str, limit: int = 12) -> list[Track]:
    stmt = (
        with_artists(filtered_feed_stmt())
        .join(TrackArtist)
        .join(Artist)
        .where(or_(Track.region == region, Artist.region == region))
        .order_by(Track.popularity_score.desc(), Track.quality_score.desc(), Track.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().unique().all())
