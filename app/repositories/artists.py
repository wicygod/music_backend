import json
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services.normalization_service import normalize_artist_name, normalize_name


def get_artist(db: Session, artist_id: int) -> Artist | None:
    return db.get(Artist, artist_id)


def list_artists(
    db: Session,
    *,
    q: str | None = None,
    region: str | None = None,
    priority: str | None = None,
    needs_review: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Artist], int]:
    stmt = select(Artist)
    count_stmt = select(func.count()).select_from(Artist)
    filters = []
    if q:
        filters.append(Artist.normalized_name.like(f"%{normalize_artist_name(q)}%"))
    if region:
        filters.append(Artist.region == region)
    if priority:
        filters.append(Artist.priority == priority)
    if needs_review is not None:
        filters.append(Artist.needs_review == needs_review)
    for item in filters:
        stmt = stmt.where(item)
        count_stmt = count_stmt.where(item)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(Artist.priority.asc(), Artist.name.asc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all()), total


def get_artist_track_count(db: Session, artist_id: int) -> int:
    stmt = select(func.count()).select_from(TrackArtist).where(TrackArtist.artist_id == artist_id)
    return db.execute(stmt).scalar_one()


def get_artist_tracks(db: Session, artist_id: int) -> list[Track]:
    stmt = (
        select(Track)
        .join(TrackArtist)
        .where(TrackArtist.artist_id == artist_id)
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(Track.created_at.desc())
    )
    return list(db.execute(stmt).scalars().unique().all())


def find_artist_by_normalized_name(db: Session, normalized_name: str) -> Artist | None:
    stmt = select(Artist).where(Artist.normalized_name == normalized_name)
    return db.execute(stmt).scalars().first()


def find_or_create_artist(
    db: Session,
    *,
    name: str,
    region: str = "unknown",
    avatar_url: str | None = None,
    genres: list[str] | None = None,
    source_name: str | None = "demo_seed",
    source_external_id: str | None = None,
    source_url: str | None = None,
    source_followers_count: int | None = None,
    source_verified: bool | None = None,
    is_canonical: bool | None = None,
    profile_resolved_at: datetime | None = None,
    needs_review: bool = False,
    priority: str = "normal",
    tracks_target: int = 25,
    seed_source: str | None = None,
    import_status: str = "pending",
) -> tuple[Artist, bool]:
    normalized_name = normalize_name(name)
    existing = find_artist_by_normalized_name(db, normalized_name)
    if existing:
        if avatar_url and _is_image_url(avatar_url):
            existing.avatar_url = avatar_url
        if source_name and (is_canonical or not existing.source_name):
            existing.source_name = source_name
        if source_external_id and (is_canonical or not existing.source_external_id):
            existing.source_external_id = source_external_id
        if source_url and (is_canonical or not existing.source_url):
            existing.source_url = source_url
        if source_followers_count is not None:
            existing.source_followers_count = max(0, int(source_followers_count))
        if source_verified is not None:
            existing.source_verified = bool(source_verified)
        if is_canonical is not None:
            existing.is_canonical = bool(is_canonical)
            if is_canonical:
                existing.needs_review = False
        if profile_resolved_at is not None:
            existing.profile_resolved_at = profile_resolved_at
        existing.needs_review = existing.needs_review or needs_review
        existing.priority = priority if existing.priority in {"normal", "low", "unknown"} else existing.priority
        existing.tracks_target = max(existing.tracks_target, tracks_target)
        existing.seed_source = existing.seed_source or seed_source
        existing.import_status = existing.import_status or import_status
        return existing, False

    artist = Artist(
        name=name.strip(),
        normalized_name=normalized_name,
        avatar_url=avatar_url if _is_image_url(avatar_url) else None,
        region=region,
        genres_json=json.dumps(genres or []),
        source_name=source_name,
        source_external_id=source_external_id,
        source_url=source_url,
        source_followers_count=max(0, int(source_followers_count or 0)),
        source_verified=bool(source_verified),
        is_canonical=bool(is_canonical),
        profile_resolved_at=profile_resolved_at,
        confidence_score=1.0,
        needs_review=needs_review,
        priority=priority,
        tracks_target=tracks_target,
        seed_source=seed_source,
        import_status=import_status,
    )
    db.add(artist)
    db.flush()
    return artist, True


def _is_image_url(value: str | None) -> bool:
    raw = str(value or "").strip()
    if raw.startswith("data:image/"):
        return True
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    path = parsed.path.lower()
    return (
        path.endswith((".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"))
        or "sndcdn.com" in parsed.hostname.lower()
        or "mzstatic.com" in parsed.hostname.lower()
    )
