import json
import random

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
POPULAR_POOL_TARGET = 220
POPULAR_TOP_HEAD_LIMIT = 36
POPULAR_HEAD_PER_ARTIST_LIMIT = 2
POPULAR_TOTAL_PER_ARTIST_LIMIT = 28
POPULAR_OTHER_PER_ARTIST_LIMIT = 4
POPULAR_RARE_LIMIT = 2
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
    token_filters = [
        or_(
            Track.normalized_title.like(f"%{token}%"),
            Artist.normalized_name.like(f"%{token}%"),
            Track.genre.like(f"%{token}%"),
            Track.tags_json.like(f"%{token}%"),
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
                Track.normalized_title.like(title_pattern),
                Artist.normalized_name.like(pattern),
                Track.genre.like(pattern),
                Track.tags_json.like(pattern),
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


def list_trending_tracks(db: Session, limit: int = 12) -> list[Track]:
    requested_limit = max(1, min(int(limit), 200))
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

    top_priority: list[Track] = []
    rare_mix: list[Track] = []
    other: list[Track] = []
    seen_song_keys: set[str] = set()
    duplicates_skipped = 0
    dirty_skipped = 0

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
        if song_key in seen_song_keys:
            duplicates_skipped += 1
            continue
        seen_song_keys.add(song_key)

        rank = _popular_artist_rank(display_artist)
        if rank == "top":
            top_priority.append(track)
        elif rank == "rare":
            rare_mix.append(track)
        else:
            other.append(track)

        if len(top_priority) + len(rare_mix) + len(other) >= POPULAR_POOL_TARGET:
            break

    random.shuffle(top_priority)
    random.shuffle(rare_mix)
    random.shuffle(other)
    top_pool_count = len(top_priority)
    rare_pool_count = len(rare_mix)
    other_pool_count = len(other)

    artist_counts: dict[str, int] = {}
    head_target = min(POPULAR_TOP_HEAD_LIMIT, requested_limit)
    selected, top_rest = _take_with_artist_cap(
        top_priority,
        head_target,
        artist_counts,
        POPULAR_HEAD_PER_ARTIST_LIMIT,
    )
    if len(selected) < head_target:
        fill, top_rest = _take_with_artist_cap(
            top_rest,
            head_target - len(selected),
            artist_counts,
            POPULAR_TOTAL_PER_ARTIST_LIMIT,
        )
        selected.extend(fill)

    remaining_slots = requested_limit - len(selected)
    rare_budget = 0
    if rare_mix and remaining_slots > 0:
        rare_roll = random.random()
        rare_budget = 2 if rare_roll < 0.08 else 1 if rare_roll < 0.35 else 0
        rare_budget = min(rare_budget, POPULAR_RARE_LIMIT, remaining_slots)

    if remaining_slots - rare_budget > 0:
        top_tail, top_rest = _take_with_artist_cap(
            top_rest,
            remaining_slots - rare_budget,
            artist_counts,
            POPULAR_TOTAL_PER_ARTIST_LIMIT,
        )
        selected.extend(top_tail)

    remaining_slots = requested_limit - len(selected)
    rare_take: list[Track] = []
    if rare_budget > 0 and remaining_slots > 0:
        rare_take, rare_mix = _take_with_artist_cap(
            rare_mix,
            min(rare_budget, remaining_slots),
            artist_counts,
            POPULAR_RARE_LIMIT,
        )
        selected.extend(rare_take)

    remaining_slots = requested_limit - len(selected)
    if remaining_slots > 0:
        other_take, other_rest = _take_with_artist_cap(
            other,
            remaining_slots,
            artist_counts,
            POPULAR_OTHER_PER_ARTIST_LIMIT,
        )
        selected.extend(other_take)
        remaining_slots = requested_limit - len(selected)
        if remaining_slots > 0:
            overflow = top_rest + rare_mix + other_rest
            random.shuffle(overflow)
            selected.extend(overflow[:remaining_slots])

    selected_top = sum(1 for track in selected if _popular_artist_rank(_display_artist_for_popular(track)) == "top")
    selected_rare = sum(1 for track in selected if _popular_artist_rank(_display_artist_for_popular(track)) == "rare")
    selected_other = len(selected) - selected_top - selected_rare
    print(
        "[POPULAR] "
        f"pool={top_pool_count + rare_pool_count + other_pool_count} "
        f"selected={len(selected)} selected_top={selected_top} selected_rare={selected_rare} selected_other={selected_other} "
        f"top_pool={top_pool_count} rare_pool={rare_pool_count} other_pool={other_pool_count} "
        f"duplicates_skipped={duplicates_skipped} dirty_skipped={dirty_skipped}",
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


def _take_with_artist_cap(
    tracks: list[Track],
    limit: int,
    artist_counts: dict[str, int],
    per_artist_limit: int,
) -> tuple[list[Track], list[Track]]:
    taken: list[Track] = []
    rest: list[Track] = []
    for track in tracks:
        artist_key = _popular_artist_key(track)
        if len(taken) < limit and artist_counts.get(artist_key, 0) < per_artist_limit:
            taken.append(track)
            artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        else:
            rest.append(track)
    return taken, rest


def _popular_artist_rank(artist_name: str) -> str:
    normalized = normalize_name(artist_name)
    if any(name and (normalized == name or name in normalized or normalized in name) for name in TOP_PRIORITY_ARTISTS):
        return "top"
    if any(name and (normalized == name or name in normalized or normalized in name) for name in RARE_MIX_ARTISTS):
        return "rare"
    return "other"


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
