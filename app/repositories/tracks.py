import json
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.schemas.track import TrackSeedCreate
from app.services.artist_cleanup_service import (
    artist_from_title,
    has_clean_artist_signal,
    popular_track_key,
    primary_artist_segment,
)
from app.services.normalization_service import normalize_name, normalize_title, normalize_track_title_for_dedupe
from app.services.popular_ranking_service import PopularCandidate, rank_popular_candidates


TOP_PRIORITY_ARTISTS = tuple(
    normalize_name(name)
    for name in (
        "Lil Peep",
        "9 mice",
        "Kai Angel",
        "Viperr",
        "Pharaoh",
        "\u0422\u0451\u043c\u043d\u044b\u0439 \u041f\u0440\u0438\u043d\u0446",
        "fortuna812",
        "Face",
        "cupsize",
        "madkid",
        "\u0441\u043d\u044f\u043b\u0446\u0435\u043f\u0438",
    )
)
RARE_MIX_ARTISTS = tuple(normalize_name(name) for name in ("tuborosho", "anonymous ember"))
POPULAR_POOL_SCAN_LIMIT = 2200
POPULAR_POOL_TARGET = 400
POPULAR_POOL_PER_ARTIST_LIMIT = 12
POPULAR_BLOCKED_PHRASES = (
    "home loan",
    "loan",
    "eligibility",
    "calculator",
    "mortgage",
    "insurance",
    "heart rate",
    "throat",
    "stethoscope",
    "tuning fork",
    "color analysis",
    "light tracking",
    "medical",
    "doctor",
    "checkup",
    "check up",
)


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
        Track.is_playable == True,
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


def search_tracks(db: Session, query: str, limit: int = 50) -> list[Track]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []
    pattern = f"%{normalized_query}%"
    title_pattern = f"%{normalize_title(query)}%"
    normalized_track_title = func.replace(Track.normalized_title, "ё", "е")
    normalized_artist_name = func.replace(Artist.normalized_name, "ё", "е")
    normalized_genre = func.replace(Track.genre, "ё", "е")
    normalized_tags = func.replace(Track.tags_json, "ё", "е")
    token_filters = [
        or_(
            normalized_track_title.like(f"%{token}%"),
            normalized_artist_name.like(f"%{token}%"),
            normalized_genre.like(f"%{token}%"),
            normalized_tags.like(f"%{token}%"),
        )
        for token in normalized_query.split()
        if len(token) > 1
    ]
    token_match = and_(*token_filters) if token_filters else False
    stmt = (
        with_artists(select(Track))
        .join(TrackArtist)
        .join(Artist)
        .where(
            or_(
                normalized_track_title.like(title_pattern),
                normalized_artist_name.like(pattern),
                normalized_genre.like(pattern),
                normalized_tags.like(pattern),
                token_match,
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


def list_trending_tracks(
    db: Session,
    limit: int = 12,
    rotation_key: str = "local",
    excluded_song_keys: set[str] | None = None,
) -> list[Track]:
    requested_limit = max(1, min(int(limit), 120))
    stmt = (
        with_artists(filtered_feed_stmt())
        .join(TrackArtist)
        .join(Artist)
        .where(
            Track.is_playable == True,
            Track.source_name.in_(["soundcloud", "youtube", "youtube_music", "yt", "sc"]),
        )
        .order_by(Track.popularity_score.desc(), Track.quality_score.desc(), Track.created_at.desc())
        .limit(POPULAR_POOL_SCAN_LIMIT)
    )
    candidates = list(db.execute(stmt).scalars().unique().all())

    popular_candidates: list[PopularCandidate[Track]] = []
    seen_song_keys: set[str] = set()
    pool_artist_counts: dict[str, int] = {}
    duplicates_skipped = 0
    dirty_skipped = 0
    artist_cap_skipped = 0

    for track in candidates:
        artist = _primary_artist_name(track)
        priority_artist = _priority_artist_from_text(track.title, artist)
        display_artist = priority_artist or artist_from_title(track.title) or artist
        if _is_blocked_popular_candidate(track.title, display_artist):
            dirty_skipped += 1
            continue
        if not display_artist or not (priority_artist or has_clean_artist_signal(track.title, display_artist, track.source_url)):
            dirty_skipped += 1
            continue

        song_key = popular_track_key(track.title, display_artist)
        if not song_key:
            dirty_skipped += 1
            continue
        if excluded_song_keys and song_key in excluded_song_keys:
            continue
        if song_key in seen_song_keys:
            duplicates_skipped += 1
            continue
        seen_song_keys.add(song_key)

        artist_key = _popular_artist_key(track)
        if pool_artist_counts.get(artist_key, 0) >= POPULAR_POOL_PER_ARTIST_LIMIT:
            artist_cap_skipped += 1
            continue
        pool_artist_counts[artist_key] = pool_artist_counts.get(artist_key, 0) + 1
        popular_candidates.append(
            PopularCandidate(
                item=track,
                stable_key=str(track.id),
                artist_key=artist_key,
                genre_key=_popular_genre_key(track),
                region_key=normalize_name(track.region or "unknown") or "unknown",
                popularity_score=track.popularity_score,
                quality_score=track.quality_score,
            )
        )

        if len(popular_candidates) >= POPULAR_POOL_TARGET:
            break

    rotation_seed = f"{datetime.now(timezone.utc).date().isoformat()}:{rotation_key or 'local'}"
    ranked = rank_popular_candidates(
        popular_candidates,
        limit=requested_limit,
        rotation_key=rotation_seed,
    )
    selected = [candidate.item for candidate in ranked]
    head = ranked[: min(24, len(ranked))]
    head_artist_counts: dict[str, int] = {}
    for candidate in head:
        head_artist_counts[candidate.artist_key] = head_artist_counts.get(candidate.artist_key, 0) + 1
    print(
        "[POPULAR] "
        f"pool={len(popular_candidates)} selected={len(selected)} "
        f"head_unique_artists={len(head_artist_counts)} "
        f"head_max_per_artist={max(head_artist_counts.values(), default=0)} "
        f"duplicates_skipped={duplicates_skipped} dirty_skipped={dirty_skipped} "
        f"artist_cap_skipped={artist_cap_skipped}",
        flush=True,
    )
    return selected


def _primary_artist_name(track: Track) -> str:
    links = sorted(track.artist_links, key=lambda item: 0 if item.role == "main" else 1)
    for link in links:
        if link.artist and link.artist.name:
            return link.artist.name
    return ""


def _display_artist_for_popular(track: Track) -> str:
    artist = _primary_artist_name(track)
    return _priority_artist_from_text(track.title, artist) or artist_from_title(track.title) or artist


def _priority_artist_from_text(title: str | None, artist: str | None = None) -> str:
    haystack = normalize_name(f"{title or ''} {artist or ''}")
    for name in TOP_PRIORITY_ARTISTS + RARE_MIX_ARTISTS:
        if name and (haystack == name or name in haystack):
            return name
    return ""


def _is_blocked_popular_candidate(title: str | None, artist: str | None = None) -> bool:
    haystack = normalize_name(f"{title or ''} {artist or ''}")
    return any(phrase in haystack for phrase in POPULAR_BLOCKED_PHRASES)


def _popular_artist_key(track: Track) -> str:
    display_artist = _display_artist_for_popular(track)
    normalized = normalize_name(display_artist)
    for name in TOP_PRIORITY_ARTISTS + RARE_MIX_ARTISTS:
        if name and (normalized == name or name in normalized or normalized in name):
            return name
    return normalize_name(primary_artist_segment(display_artist)) or normalized or "unknown"


def _popular_genre_key(track: Track) -> str:
    normalized = normalize_name(track.genre or "")
    if not normalized:
        return "unknown"
    if any(token in normalized for token in ("soundcloud", "youtube", "http", "www", "unknown")):
        return "unknown"
    return normalized[:64]


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
