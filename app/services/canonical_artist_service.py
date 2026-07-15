from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services.normalization_service import normalize_artist_name
from app.services.soundcloud_profile_service import (
    SoundCloudProfile,
    canonical_soundcloud_profile_url,
    resolve_canonical_soundcloud_profile,
)


PROFILE_REFRESH_INTERVAL = timedelta(days=30)
PROFILE_RESOLUTION_TIMEOUT_SECONDS = 12.0


def refresh_canonical_artist_for_search(
    db: Session,
    search: str,
    *,
    force: bool = False,
) -> Artist | None:
    """Resolve an exact catalog identity before serving onboarding search.

    Partial text queries stay database-only. A full artist name can trigger one
    bounded SoundCloud lookup, which is then persisted and reused for 30 days.
    """

    normalized = normalize_artist_name(search or "").strip()
    if not normalized:
        return None
    artists = list(
        db.execute(
            select(Artist)
            .where(Artist.normalized_name == normalized)
            .order_by(
                case((Artist.priority == "high", 0), (Artist.priority == "normal", 1), else_=2),
                case((Artist.seed_source.is_not(None), 0), else_=1),
                Artist.id.asc(),
            )
        ).scalars().all()
    )
    if not artists:
        return None

    track_counts = dict(
        db.execute(
            select(TrackArtist.artist_id, func.count(TrackArtist.track_id))
            .where(TrackArtist.artist_id.in_([artist.id for artist in artists]))
            .group_by(TrackArtist.artist_id)
        ).all()
    )
    primary = max(
        artists,
        key=lambda artist: (
            artist.priority == "high",
            bool(artist.seed_source),
            bool(artist.is_canonical),
            int(track_counts.get(artist.id, 0)),
            -artist.id,
        ),
    )
    now = datetime.utcnow()
    if not force and _profile_is_fresh(primary, now):
        return primary

    candidate_urls = _candidate_profile_urls(db, artists)
    profile = resolve_canonical_soundcloud_profile(
        primary.name,
        candidate_urls,
        include_search=True,
        max_candidates=12,
        timeout=PROFILE_RESOLUTION_TIMEOUT_SECONDS,
    )
    if profile is None or normalize_artist_name(profile.username) != normalized:
        primary.profile_resolved_at = now
        db.flush()
        return None

    for artist in artists:
        artist.is_canonical = False
    apply_canonical_profile(primary, profile, resolved_at=now)
    db.flush()
    return primary


def apply_canonical_profile(
    artist: Artist,
    profile: SoundCloudProfile,
    *,
    resolved_at: datetime | None = None,
) -> None:
    """Persist provider identity without changing the catalog display name."""

    artist.source_name = "soundcloud"
    artist.source_external_id = profile.external_id
    artist.source_url = profile.permalink_url
    artist.avatar_url = profile.avatar_url
    artist.source_followers_count = max(0, int(profile.followers_count or 0))
    artist.source_verified = bool(profile.verified)
    artist.is_canonical = True
    artist.profile_resolved_at = resolved_at or datetime.utcnow()
    artist.confidence_score = 1.0
    artist.needs_review = False


def _profile_is_fresh(artist: Artist, now: datetime) -> bool:
    resolved_at = artist.profile_resolved_at
    avatar = str(artist.avatar_url or "").lower()
    return bool(
        artist.is_canonical
        and resolved_at is not None
        and now - resolved_at <= PROFILE_REFRESH_INTERVAL
        and "sndcdn.com" in avatar
        and "/avatars-" in avatar
    )


def _candidate_profile_urls(db: Session, artists: list[Artist]) -> list[str]:
    candidates: list[str | None] = []
    for artist in artists:
        candidates.extend((artist.source_url, artist.avatar_url))
    candidates.extend(
        db.execute(
            select(Track.source_url)
            .join(TrackArtist, TrackArtist.track_id == Track.id)
            .where(
                TrackArtist.artist_id.in_([artist.id for artist in artists]),
                Track.source_name.in_(("soundcloud", "sc")),
                Track.source_url.is_not(None),
            )
            .order_by(Track.popularity_score.desc(), Track.id.asc())
            .limit(100)
        ).scalars().all()
    )
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        profile_url = canonical_soundcloud_profile_url(candidate)
        if profile_url is None or profile_url in seen:
            continue
        seen.add(profile_url)
        result.append(profile_url)
    return result
