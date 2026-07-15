import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.playlist import UserFavorite, UserPlaylist, UserPlaylistTrack
from app.models.track import Track, TrackArtist
from app.schemas.track import TrackSeedCreate
from app.services.artist_cleanup_service import (
    artist_from_title,
    has_clean_artist_signal,
    is_low_value_popular_variant,
    popular_track_key,
    primary_artist_segment,
)
from app.services.normalization_service import normalize_name, normalize_title, normalize_track_title_for_dedupe
from app.services.popular_ranking_service import (
    POPULAR_RANKING_CONFIG,
    PROVIDER_POPULARITY_TAG,
    PopularCandidate,
    is_popular_candidate_eligible,
    popular_candidate_score,
    rank_popular_candidates,
)


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
POPULAR_MIN_DURATION_SECONDS = 45
POPULAR_MAX_DURATION_SECONDS = 12 * 60
POPULAR_UNKNOWN_PROVIDER_SCORES = {50.0, 65.0, 75.0}
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
    return [
        candidate.item
        for candidate in list_trending_rankings(
            db,
            limit=limit,
            rotation_key=rotation_key,
            excluded_song_keys=excluded_song_keys,
        )
    ]


def list_trending_rankings(
    db: Session,
    limit: int = 12,
    rotation_key: str = "local",
    excluded_song_keys: set[str] | None = None,
) -> list[PopularCandidate[Track]]:
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

    canonical_by_name = _canonical_popular_artist_index(db)
    preliminary: list[tuple[Track, Artist, str, str]] = []
    seen_song_keys: set[str] = set()
    duplicates_skipped = 0
    dirty_skipped = 0
    authority_skipped = 0

    for track in candidates:
        if not _popular_track_quality_eligible(track):
            dirty_skipped += 1
            continue
        authority = _effective_popular_artist(track, canonical_by_name)
        if authority is None:
            authority_skipped += 1
            continue
        display_artist = authority.name
        if _is_blocked_popular_candidate(track.title, display_artist):
            dirty_skipped += 1
            continue
        if not has_clean_artist_signal(track.title, display_artist, track.source_url):
            dirty_skipped += 1
            continue

        song_key = popular_track_key(track.title, display_artist)
        if not song_key:
            dirty_skipped += 1
            continue
        if excluded_song_keys and song_key in excluded_song_keys:
            continue
        preliminary.append((track, authority, song_key, normalize_name(authority.name) or "unknown"))

    signal_maps = _popular_signal_maps(
        db,
        [track.id for track, _authority, _song_key, _artist_key in preliminary],
    )

    popular_candidates: list[PopularCandidate[Track]] = []
    pool_artist_counts: dict[str, int] = {}
    artist_cap_skipped = 0
    evidence_skipped = 0

    for track, authority, song_key, artist_key in preliminary:
        stats = signal_maps.get(track.id, {})
        if pool_artist_counts.get(artist_key, 0) >= POPULAR_POOL_PER_ARTIST_LIMIT:
            artist_cap_skipped += 1
            continue
        candidate = PopularCandidate(
            item=track,
            stable_key=str(track.id),
            artist_key=artist_key,
            genre_key=_popular_genre_key(track),
            region_key=normalize_name(track.region or "unknown") or "unknown",
            popularity_score=float(track.popularity_score or 0.0),
            quality_score=float(track.quality_score or 0.0),
            provider_signal_reliable=_provider_popularity_is_reliable(track),
            artist_followers=max(0, int(authority.source_followers_count or 0)),
            artist_verified=bool(authority.source_verified),
            artist_canonical=bool(authority.is_canonical and not authority.needs_review),
            unique_listeners=int(stats.get("unique_listeners", 0)),
            capped_plays=int(stats.get("capped_plays", 0)),
            recent_plays=int(stats.get("recent_plays", 0)),
            detailed_plays=int(stats.get("detailed_plays", 0)),
            completed_plays=int(stats.get("completed_plays", 0)),
            skipped_plays=int(stats.get("skipped_plays", 0)),
            favorite_count=int(stats.get("favorite_count", 0)),
            playlist_add_count=int(stats.get("playlist_add_count", 0)),
            low_value_variant=is_low_value_popular_variant(track.title),
        )
        if not is_popular_candidate_eligible(candidate):
            evidence_skipped += 1
            continue
        # Only an eligible candidate may reserve the normalized song key.
        # Otherwise a rejected slowed/remix reupload scanned first could hide
        # the clean original from the chart.
        if song_key in seen_song_keys:
            duplicates_skipped += 1
            continue
        seen_song_keys.add(song_key)
        pool_artist_counts[artist_key] = pool_artist_counts.get(artist_key, 0) + 1
        popular_candidates.append(candidate)

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
        f"authority_skipped={authority_skipped} evidence_skipped={evidence_skipped} "
        f"artist_cap_skipped={artist_cap_skipped}",
        flush=True,
    )
    return ranked


def popular_ranking_payload(candidate: PopularCandidate[Track], position: int) -> dict:
    """Expose explainable chart metrics to the authenticated admin app."""

    detailed = max(0, int(candidate.detailed_plays or 0))
    completion_rate = candidate.completed_plays / detailed if detailed else None
    skip_rate = candidate.skipped_plays / detailed if detailed else None
    return {
        "track": candidate.item,
        "position": max(1, int(position)),
        "score": popular_candidate_score(candidate),
        "algorithm_version": "popular-v2",
        "provider_score": float(candidate.popularity_score or 0.0),
        "provider_signal_reliable": bool(candidate.provider_signal_reliable),
        "artist_followers": max(0, int(candidate.artist_followers or 0)),
        "artist_verified": bool(candidate.artist_verified),
        "unique_listeners": max(0, int(candidate.unique_listeners or 0)),
        "play_count": max(0, int(candidate.capped_plays or 0)),
        "repeat_plays": max(0, int(candidate.capped_plays or 0) - int(candidate.unique_listeners or 0)),
        "recent_plays": max(0, int(candidate.recent_plays or 0)),
        "completion_rate": round(completion_rate, 4) if completion_rate is not None else None,
        "skip_rate": round(skip_rate, 4) if skip_rate is not None else None,
        "favorite_count": max(0, int(candidate.favorite_count or 0)),
        "playlist_add_count": max(0, int(candidate.playlist_add_count or 0)),
    }


def _canonical_popular_artist_index(db: Session) -> dict[str, Artist]:
    profiles = list(
        db.execute(
            select(Artist).where(
                Artist.is_canonical == True,
                Artist.needs_review == False,
            )
        ).scalars().all()
    )
    result: dict[str, Artist] = {}
    for artist in profiles:
        key = normalize_name(artist.normalized_name or artist.name)
        if not key:
            continue
        current = result.get(key)
        if current is None or _canonical_artist_strength(artist) > _canonical_artist_strength(current):
            result[key] = artist
    return result


def _canonical_artist_strength(artist: Artist) -> tuple[bool, int, float, int]:
    return (
        bool(artist.source_verified),
        max(0, int(artist.source_followers_count or 0)),
        float(artist.popularity_score or 0.0),
        -int(artist.id or 0),
    )


def _effective_popular_artist(track: Track, canonical_by_name: dict[str, Artist]) -> Artist | None:
    parsed_artist = artist_from_title(track.title)
    parsed_candidates = [parsed_artist, primary_artist_segment(parsed_artist)] if parsed_artist else []
    for name in parsed_candidates:
        key = normalize_name(name or "")
        if key and key in canonical_by_name:
            return canonical_by_name[key]

    # An explicit, unresolved artist prefix is stronger evidence than the
    # uploader link.  Do not let an unrelated canonical compilation/reupload
    # profile lend its authority to the named artist in the title.
    if parsed_artist:
        return None

    # A canonical uploader link must not make an unrelated reupload official.
    # Only the declared main artist is used when the title carries no exact
    # artist prefix.  Duplicate canonical profiles are resolved by reach.
    linked = [
        link.artist
        for link in track.artist_links
        if link.role == "main"
        and link.artist
        and link.artist.is_canonical
        and not link.artist.needs_review
    ]
    return max(linked, key=_canonical_artist_strength) if linked else None


def _popular_track_quality_eligible(track: Track) -> bool:
    duration = max(0, int(track.duration_seconds or 0))
    return bool(
        track.is_playable
        and not track.needs_review
        and float(track.quality_score or 0.0) >= 70.0
        and POPULAR_MIN_DURATION_SECONDS <= duration <= POPULAR_MAX_DURATION_SECONDS
    )


def _provider_popularity_is_reliable(track: Track) -> bool:
    try:
        tags = json.loads(track.tags_json or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    if isinstance(tags, list) and PROVIDER_POPULARITY_TAG in {str(tag) for tag in tags}:
        return True
    score = round(float(track.popularity_score or 0.0), 3)
    return score > 0 and score not in POPULAR_UNKNOWN_PROVIDER_SCORES


def _popular_signal_maps(db: Session, track_ids: list[int]) -> dict[int, dict[str, int]]:
    if not track_ids:
        return {}
    result: dict[int, dict[str, int]] = {int(track_id): {} for track_id in track_ids}
    repeat_cap = max(1, int(POPULAR_RANKING_CONFIG.repeat_cap_per_user))
    cutoff = datetime.utcnow() - timedelta(days=14)
    capped_play = case(
        (ListeningHistory.play_count > repeat_cap, repeat_cap),
        else_=ListeningHistory.play_count,
    )
    recent_play = case(
        (ListeningHistory.played_at >= cutoff, capped_play),
        else_=0,
    )
    legacy_rows = db.execute(
        select(
            ListeningHistory.track_id,
            func.count(func.distinct(ListeningHistory.user_id)),
            func.coalesce(func.sum(capped_play), 0),
            func.coalesce(func.sum(recent_play), 0),
        )
        .where(
            ListeningHistory.track_id.in_(track_ids),
            ListeningHistory.event_id.is_(None),
        )
        .group_by(ListeningHistory.track_id)
    ).all()
    for track_id, unique_listeners, capped_plays, recent_plays in legacy_rows:
        result[int(track_id)].update(
            unique_listeners=int(unique_listeners or 0),
            capped_plays=int(capped_plays or 0),
            recent_plays=int(recent_plays or 0),
        )

    detailed_rows = db.execute(
        select(
            ListeningHistory.track_id,
            func.count(ListeningHistory.id),
            func.coalesce(
                func.sum(case((ListeningHistory.completed == True, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((ListeningHistory.skipped == True, 1), else_=0)),
                0,
            ),
        )
        .where(
            ListeningHistory.track_id.in_(track_ids),
            ListeningHistory.event_id.is_not(None),
        )
        .group_by(ListeningHistory.track_id)
    ).all()
    for track_id, detailed_plays, completed_plays, skipped_plays in detailed_rows:
        result[int(track_id)].update(
            detailed_plays=int(detailed_plays or 0),
            completed_plays=int(completed_plays or 0),
            skipped_plays=int(skipped_plays or 0),
        )

    favorite_rows = db.execute(
        select(
            UserFavorite.track_id,
            func.count(func.distinct(UserFavorite.user_id)),
        )
        .where(UserFavorite.track_id.in_(track_ids))
        .group_by(UserFavorite.track_id)
    ).all()
    for track_id, favorite_count in favorite_rows:
        result[int(track_id)]["favorite_count"] = int(favorite_count or 0)

    playlist_rows = db.execute(
        select(
            UserPlaylistTrack.track_id,
            func.count(func.distinct(UserPlaylist.user_id)),
        )
        .join(UserPlaylist, UserPlaylist.id == UserPlaylistTrack.playlist_id)
        .where(UserPlaylistTrack.track_id.in_(track_ids))
        .group_by(UserPlaylistTrack.track_id)
    ).all()
    for track_id, playlist_add_count in playlist_rows:
        result[int(track_id)]["playlist_add_count"] = int(playlist_add_count or 0)
    return result


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
