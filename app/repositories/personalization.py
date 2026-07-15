from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp
from typing import Iterable

from sqlalchemy import case, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.services.normalization_service import normalize_artist_name
from app.services.recommendation_config import RECOMMENDATION_CONFIG


_UNKNOWN_GENRE_VALUES = (
    "",
    "unknown",
    "soundcloud",
    "youtube",
    "youtube music",
    "youtube_music",
    "itunes",
    "provider",
)


@dataclass(frozen=True)
class OnboardingArtistRow:
    id: int
    name: str
    avatar_url: str | None
    genres: list[str]
    popularity_score: float
    track_count: int
    selected: bool


@dataclass(frozen=True)
class ExplicitPreferenceDiff:
    selected_artist_ids: list[int]
    added_artist_ids: list[int]
    removed_artist_ids: list[int]


def account_user_scope(user_id: int) -> str:
    """Return the legacy account scope used by history/favorite tables."""

    return f"account:{int(user_id)}"


def account_id_from_scope(user_scope: str | None) -> int | None:
    prefix = "account:"
    value = str(user_scope or "")
    if not value.startswith(prefix):
        return None
    try:
        account_id = int(value[len(prefix):])
    except ValueError:
        return None
    return account_id if account_id > 0 else None


def primary_artist_id_for_track(db: Session, track_id: int) -> int | None:
    stmt = (
        select(TrackArtist.artist_id)
        .where(TrackArtist.track_id == int(track_id))
        .order_by(
            case((TrackArtist.role == "main", 0), else_=1),
            TrackArtist.artist_id.asc(),
        )
        .limit(1)
    )
    value = db.execute(stmt).scalar_one_or_none()
    return int(value) if value is not None else None


def list_onboarding_artists(
    db: Session,
    *,
    user_id: int,
    search: str | None = None,
    page: int = 1,
    limit: int | None = None,
    genre: str | None = None,
) -> tuple[list[OnboardingArtistRow], int]:
    """Return a stable, diverse page without materializing the artist catalog.

    Eligible tracks use the same safety boundary as the home feed. A SQL window
    interleaves the best artist from each real genre, then the second best, and
    so on. This keeps pagination deterministic while avoiding an unbounded
    Python-side candidate load.
    """

    safe_page = max(1, int(page))
    configured_limit = limit or RECOMMENDATION_CONFIG.onboarding_page_size
    safe_limit = max(1, min(int(configured_limit), 100))
    genre_key = _normalize_genre_filter(genre)

    eligible_tracks = (
        select(
            TrackArtist.artist_id.label("artist_id"),
            Track.id.label("track_id"),
            Track.cover_url.label("cover_url"),
            Track.popularity_score.label("catalog_popularity"),
            _genre_key_expression().label("genre_key"),
        )
        .join(Track, Track.id == TrackArtist.track_id)
        .where(*_playable_track_conditions())
        .distinct()
        .subquery("onboarding_eligible_tracks")
    )

    artist_metrics = (
        select(
            eligible_tracks.c.artist_id,
            func.count(eligible_tracks.c.track_id).label("track_count"),
            func.avg(eligible_tracks.c.catalog_popularity).label("catalog_popularity"),
        )
        .group_by(eligible_tracks.c.artist_id)
        .subquery("onboarding_artist_metrics")
    )

    genre_counts = (
        select(
            eligible_tracks.c.artist_id,
            eligible_tracks.c.genre_key,
            func.count(eligible_tracks.c.track_id).label("genre_track_count"),
        )
        .where(eligible_tracks.c.genre_key != "unknown")
        .group_by(eligible_tracks.c.artist_id, eligible_tracks.c.genre_key)
        .subquery("onboarding_genre_counts")
    )
    ranked_genres = (
        select(
            genre_counts.c.artist_id,
            genre_counts.c.genre_key,
            func.row_number()
            .over(
                partition_by=genre_counts.c.artist_id,
                order_by=(
                    genre_counts.c.genre_track_count.desc(),
                    genre_counts.c.genre_key.asc(),
                ),
            )
            .label("genre_rank"),
        )
        .subquery("onboarding_ranked_genres")
    )
    primary_genres = (
        select(ranked_genres.c.artist_id, ranked_genres.c.genre_key)
        .where(ranked_genres.c.genre_rank == 1)
        .subquery("onboarding_primary_genres")
    )

    legacy_conditions = []
    event_id_column = getattr(ListeningHistory, "event_id", None)
    if event_id_column is not None:
        legacy_conditions.append(event_id_column.is_(None))
    legacy_popularity = (
        select(
            TrackArtist.artist_id.label("artist_id"),
            func.coalesce(func.sum(ListeningHistory.play_count), 0).label("legacy_plays"),
        )
        .join(ListeningHistory, ListeningHistory.track_id == TrackArtist.track_id)
        .where(*legacy_conditions)
        .group_by(TrackArtist.artist_id)
        .subquery("onboarding_legacy_popularity")
    )

    selected = exists(
        select(literal(1)).where(
            UserArtistPreference.user_id == int(user_id),
            UserArtistPreference.artist_id == Artist.id,
            UserArtistPreference.explicit_selected.is_(True),
        )
    )
    promoted_score = case((Artist.priority == "high", 1), else_=0)
    candidate_stmt = (
        select(
            Artist.id.label("artist_id"),
            Artist.name.label("artist_name"),
            Artist.normalized_name.label("artist_normalized_name"),
            Artist.avatar_url.label("avatar_url"),
            func.coalesce(primary_genres.c.genre_key, "unknown").label("primary_genre"),
            artist_metrics.c.track_count,
            func.coalesce(legacy_popularity.c.legacy_plays, 0).label("legacy_plays"),
            func.coalesce(artist_metrics.c.catalog_popularity, 0.0).label("catalog_popularity"),
            Artist.source_followers_count.label("source_followers_count"),
            Artist.source_verified.label("source_verified"),
            promoted_score.label("promoted_score"),
            selected.label("selected"),
        )
        .join(artist_metrics, artist_metrics.c.artist_id == Artist.id)
        .outerjoin(primary_genres, primary_genres.c.artist_id == Artist.id)
        .outerjoin(legacy_popularity, legacy_popularity.c.artist_id == Artist.id)
        .where(
            Artist.needs_review.is_(False),
            Artist.is_canonical.is_(True),
        )
    )

    clean_search = normalize_artist_name(search or "").strip()
    exact_match_score = literal(0)
    prefix_match_score = literal(0)
    if clean_search:
        escaped_search = _escape_like(clean_search)
        candidate_stmt = candidate_stmt.where(
            Artist.normalized_name.like(f"%{escaped_search}%", escape="\\")
        )
        exact_match_score = case((Artist.normalized_name == clean_search, 1), else_=0)
        prefix_match_score = case(
            (Artist.normalized_name.like(f"{escaped_search}%", escape="\\"), 1),
            else_=0,
        )
        candidate_stmt = candidate_stmt.add_columns(
            exact_match_score.label("exact_match_score"),
            prefix_match_score.label("prefix_match_score"),
        )
    else:
        candidate_stmt = candidate_stmt.where(
            Artist.source_followers_count
            >= RECOMMENDATION_CONFIG.onboarding_min_profile_followers
        )
    if genre_key:
        candidate_stmt = candidate_stmt.where(
            exists(
                select(literal(1)).where(
                    eligible_tracks.c.artist_id == Artist.id,
                    eligible_tracks.c.genre_key == genre_key,
                )
            )
        )

    raw_candidates = candidate_stmt.subquery("onboarding_raw_candidates")
    canonical_identity_rank = func.row_number().over(
        partition_by=raw_candidates.c.artist_normalized_name,
        order_by=(
            raw_candidates.c.selected.desc(),
            raw_candidates.c.source_verified.desc(),
            raw_candidates.c.source_followers_count.desc(),
            raw_candidates.c.legacy_plays.desc(),
            raw_candidates.c.catalog_popularity.desc(),
            raw_candidates.c.track_count.desc(),
            raw_candidates.c.artist_id.asc(),
        ),
    )
    ranked_identities = select(
        raw_candidates,
        canonical_identity_rank.label("canonical_identity_rank"),
    ).subquery("onboarding_ranked_identities")
    candidates = (
        select(ranked_identities)
        .where(ranked_identities.c.canonical_identity_rank == 1)
        .subquery("onboarding_candidates")
    )

    if clean_search:
        # Search is a disambiguation flow, not a paginated catalog listing.
        # Always expose the single strongest canonical profile so a solo exact
        # match cannot be displaced by a more-followed collaboration or fan
        # account whose display name merely contains the query.
        best_match_stmt = (
            select(candidates)
            .order_by(
                candidates.c.exact_match_score.desc(),
                candidates.c.prefix_match_score.desc(),
                candidates.c.source_verified.desc(),
                candidates.c.source_followers_count.desc(),
                candidates.c.legacy_plays.desc(),
                candidates.c.catalog_popularity.desc(),
                candidates.c.track_count.desc(),
                candidates.c.artist_id.asc(),
            )
            .limit(1)
        )
        best_rows = list(db.execute(best_match_stmt).mappings().all())
        total = 1 if best_rows else 0
        page_rows = best_rows if safe_page == 1 else []
        artist_ids = [int(row["artist_id"]) for row in page_rows]
        genres_by_artist = _load_page_genres(db, eligible_tracks, artist_ids)
        return (
            [_onboarding_artist_row(row, genres_by_artist) for row in page_rows],
            total,
        )

    total = int(db.execute(select(func.count()).select_from(candidates)).scalar_one())

    diversity_rank = func.row_number().over(
        partition_by=candidates.c.primary_genre,
        order_by=(
            candidates.c.selected.desc(),
            candidates.c.source_verified.desc(),
            candidates.c.source_followers_count.desc(),
            candidates.c.legacy_plays.desc(),
            candidates.c.promoted_score.desc(),
            candidates.c.catalog_popularity.desc(),
            candidates.c.track_count.desc(),
            candidates.c.artist_id.asc(),
        ),
    )
    ranked = select(candidates, diversity_rank.label("diversity_rank")).subquery("onboarding_diverse")
    page_stmt = (
        select(ranked)
        .order_by(
            ranked.c.diversity_rank.asc(),
            ranked.c.selected.desc(),
            ranked.c.source_verified.desc(),
            ranked.c.source_followers_count.desc(),
            ranked.c.legacy_plays.desc(),
            ranked.c.promoted_score.desc(),
            ranked.c.catalog_popularity.desc(),
            ranked.c.track_count.desc(),
            ranked.c.primary_genre.asc(),
            ranked.c.artist_id.asc(),
        )
        .limit(safe_limit)
        .offset((safe_page - 1) * safe_limit)
    )
    page_rows = list(db.execute(page_stmt).mappings().all())
    artist_ids = [int(row["artist_id"]) for row in page_rows]
    genres_by_artist = _load_page_genres(db, eligible_tracks, artist_ids)

    return ([_onboarding_artist_row(row, genres_by_artist) for row in page_rows], total)


def _onboarding_artist_row(row, genres_by_artist: dict[int, list[str]]) -> OnboardingArtistRow:
    artist_id = int(row["artist_id"])
    return OnboardingArtistRow(
        id=artist_id,
        name=str(row["artist_name"]),
        avatar_url=row["avatar_url"],
        genres=genres_by_artist.get(artist_id, []),
        popularity_score=float(row["source_followers_count"] or 0),
        track_count=int(row["track_count"] or 0),
        selected=bool(row["selected"]),
    )


def get_user_artist_preference(
    db: Session,
    *,
    user_id: int,
    artist_id: int,
    for_update: bool = False,
) -> UserArtistPreference | None:
    stmt = select(UserArtistPreference).where(
        UserArtistPreference.user_id == int(user_id),
        UserArtistPreference.artist_id == int(artist_id),
    )
    if for_update and db.bind is not None and db.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def list_user_artist_preferences(
    db: Session,
    *,
    user_id: int,
    explicit_only: bool = False,
    include_hidden: bool = True,
) -> list[UserArtistPreference]:
    stmt = select(UserArtistPreference).where(UserArtistPreference.user_id == int(user_id))
    if explicit_only:
        stmt = stmt.where(UserArtistPreference.explicit_selected.is_(True))
    if not include_hidden:
        stmt = stmt.where(UserArtistPreference.is_hidden.is_(False))
    stmt = stmt.order_by(UserArtistPreference.weight.desc(), UserArtistPreference.artist_id.asc())
    return list(db.execute(stmt).scalars().all())


def selected_artist_ids(db: Session, *, user_id: int) -> list[int]:
    stmt = (
        select(UserArtistPreference.artist_id)
        .where(
            UserArtistPreference.user_id == int(user_id),
            UserArtistPreference.explicit_selected.is_(True),
        )
        .order_by(UserArtistPreference.artist_id.asc())
    )
    return [int(value) for value in db.execute(stmt).scalars().all()]


def ensure_user_artist_preference(
    db: Session,
    *,
    user_id: int,
    artist_id: int,
    source: str,
    now: datetime | None = None,
) -> UserArtistPreference:
    existing = get_user_artist_preference(
        db,
        user_id=user_id,
        artist_id=artist_id,
        for_update=True,
    )
    if existing is not None:
        return existing

    timestamp = _naive_utc(now or datetime.utcnow())
    values = {
        "user_id": int(user_id),
        "artist_id": int(artist_id),
        "source": source,
        "explicit_weight": 0.0,
        "behavior_weight": 0.0,
        "weight": 0.0,
        "explicit_selected": False,
        "is_hidden": False,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    dialect_name = db.bind.dialect.name if db.bind is not None else ""
    if dialect_name == "sqlite":
        stmt = sqlite_insert(UserArtistPreference).values(**values).on_conflict_do_nothing(
            index_elements=["user_id", "artist_id"]
        )
        db.execute(stmt)
    elif dialect_name == "postgresql":
        stmt = postgresql_insert(UserArtistPreference).values(**values).on_conflict_do_nothing(
            index_elements=["user_id", "artist_id"]
        )
        db.execute(stmt)
    else:
        db.add(UserArtistPreference(**values))
        db.flush()

    preference = get_user_artist_preference(
        db,
        user_id=user_id,
        artist_id=artist_id,
        for_update=True,
    )
    if preference is None:  # pragma: no cover - defensive for unsupported dialects.
        raise RuntimeError("Failed to upsert user artist preference")
    return preference


def replace_explicit_preferences(
    db: Session,
    *,
    user_id: int,
    artist_ids: Iterable[int],
    source: str,
    explicit_weight: float | None = None,
    now: datetime | None = None,
) -> ExplicitPreferenceDiff:
    timestamp = _naive_utc(now or datetime.utcnow())
    selected_ids = sorted({int(artist_id) for artist_id in artist_ids})
    selected_set = set(selected_ids)
    configured_weight = (
        RECOMMENDATION_CONFIG.onboarding_weight
        if explicit_weight is None
        else float(explicit_weight)
    )

    current = list_user_artist_preferences(db, user_id=user_id)
    current_explicit = {item.artist_id for item in current if item.explicit_selected}
    by_artist = {item.artist_id: item for item in current}

    for artist_id in selected_ids:
        preference = by_artist.get(artist_id) or ensure_user_artist_preference(
            db,
            user_id=user_id,
            artist_id=artist_id,
            source=source,
            now=timestamp,
        )
        _decay_behavior_weight(preference, timestamp)
        preference.explicit_selected = True
        preference.explicit_source = source
        preference.explicit_weight = _clamp(configured_weight)
        preference.weight = _combined_weight(preference)
        preference.is_hidden = False
        preference.source = source
        preference.updated_at = timestamp

    for preference in current:
        if not preference.explicit_selected or preference.artist_id in selected_set:
            continue
        _decay_behavior_weight(preference, timestamp)
        preference.explicit_selected = False
        preference.explicit_source = None
        preference.explicit_weight = 0.0
        preference.weight = _combined_weight(preference)
        preference.source = source
        preference.updated_at = timestamp

    db.flush()
    return ExplicitPreferenceDiff(
        selected_artist_ids=selected_ids,
        added_artist_ids=sorted(selected_set - current_explicit),
        removed_artist_ids=sorted(current_explicit - selected_set),
    )


def apply_preference_signal(
    db: Session,
    *,
    user_id: int,
    artist_id: int,
    source: str,
    delta: float,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
    hidden: bool | None = None,
) -> UserArtistPreference:
    timestamp = _naive_utc(now or datetime.utcnow())
    event_time = _naive_utc(occurred_at or timestamp)
    preference = ensure_user_artist_preference(
        db,
        user_id=user_id,
        artist_id=artist_id,
        source=source,
        now=timestamp,
    )
    _decay_behavior_weight(preference, timestamp)

    age_days = max(0.0, (timestamp - event_time).total_seconds() / 86_400.0)
    decay_period = max(0.001, float(RECOMMENDATION_CONFIG.decay_period_days))
    effective_delta = float(delta) * exp(-age_days / decay_period)
    preference.behavior_weight = _clamp(float(preference.behavior_weight or 0.0) + effective_delta)
    preference.weight = _combined_weight(preference)
    preference.source = source
    if hidden is not None:
        preference.is_hidden = bool(hidden)
    preference.updated_at = timestamp
    db.flush()
    return preference


def _playable_track_conditions() -> tuple:
    return (
        Track.is_playable.is_(True),
        Track.quality_score >= 60,
        Track.needs_review.is_(False),
        or_(Track.audio_src.is_not(None), Track.source_url.is_not(None)),
    )


def _genre_key_expression():
    raw_genre = func.lower(func.trim(func.coalesce(Track.genre, "")))
    return case(
        (
            or_(
                raw_genre.in_(_UNKNOWN_GENRE_VALUES),
                raw_genre.like("http%"),
                raw_genre.like("%www.%"),
            ),
            "unknown",
        ),
        else_=raw_genre,
    )


def _normalize_genre_filter(value: str | None) -> str | None:
    cleaned = str(value or "").strip().lower()
    if not cleaned or cleaned in _UNKNOWN_GENRE_VALUES or cleaned.startswith("http"):
        return None
    return cleaned


def _load_page_genres(db: Session, eligible_tracks, artist_ids: list[int]) -> dict[int, list[str]]:
    if not artist_ids:
        return {}
    stmt = (
        select(eligible_tracks.c.artist_id, eligible_tracks.c.genre_key)
        .where(
            eligible_tracks.c.artist_id.in_(artist_ids),
            eligible_tracks.c.genre_key != "unknown",
        )
        .distinct()
        .order_by(eligible_tracks.c.artist_id.asc(), eligible_tracks.c.genre_key.asc())
    )
    result: dict[int, list[str]] = {artist_id: [] for artist_id in artist_ids}
    for artist_id, genre_key in db.execute(stmt).all():
        result.setdefault(int(artist_id), []).append(str(genre_key))
    return result


def _decay_behavior_weight(preference: UserArtistPreference, now: datetime) -> None:
    updated_at = _naive_utc(preference.updated_at or now)
    age_days = max(0.0, (now - updated_at).total_seconds() / 86_400.0)
    if age_days <= 0:
        return
    decay_period = max(0.001, float(RECOMMENDATION_CONFIG.decay_period_days))
    preference.behavior_weight = _clamp(
        float(preference.behavior_weight or 0.0) * exp(-age_days / decay_period)
    )


def _combined_weight(preference: UserArtistPreference) -> float:
    return _clamp(
        float(preference.explicit_weight or 0.0)
        + float(preference.behavior_weight or 0.0)
    )


def _clamp(value: float) -> float:
    return max(
        float(RECOMMENDATION_CONFIG.minimum_preference_weight),
        min(float(RECOMMENDATION_CONFIG.maximum_preference_weight), float(value)),
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
