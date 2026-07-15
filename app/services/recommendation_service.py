from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import exp
from threading import Lock
from time import monotonic

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.repositories.tracks import filtered_feed_stmt, with_artists
from app.schemas.feed import RecommendationTrack
from app.services.personalized_ranking_service import ScoredRecommendation, choose_weighted_mix
from app.services.recommendation_config import RECOMMENDATION_CONFIG
from app.services.serialization_service import track_to_read
from app.services.track_filter_service import is_music_track


_INVALID_GENRES = {
    "",
    "unknown",
    "other",
    "soundcloud",
    "youtube",
    "youtube music",
    "youtube_music",
    "music",
}


@dataclass(frozen=True)
class RecommendationResult:
    items: list[RecommendationTrack]
    personalization_active: bool


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    result: RecommendationResult


@dataclass(frozen=True)
class _SimilarArtistMatch:
    score: float
    preferred_artist_id: int


_cache: OrderedDict[int, _CacheEntry] = OrderedDict()
_cache_lock = Lock()


def invalidate_recommendations(user_id: int | None = None) -> None:
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(int(user_id), None)


def get_personalized_recommendations(
    db: Session,
    *,
    user_id: int,
    limit: int | None = None,
) -> RecommendationResult:
    requested_limit = max(1, min(int(limit or RECOMMENDATION_CONFIG.recommendation_limit), 100))
    cached = _get_cached(user_id, requested_limit)
    if cached:
        return cached

    preferences = list(
        db.execute(
            select(UserArtistPreference).where(UserArtistPreference.user_id == user_id)
        ).scalars().all()
    )
    visible_preferences = [item for item in preferences if not item.is_hidden]
    effective_preferences = _effective_artist_preferences(visible_preferences)
    personalization_active = any(weight > 0 for weight in effective_preferences.values())
    explicit_artist_ids = {
        item.artist_id for item in visible_preferences if item.explicit_selected
    }
    explicit_artist_sources = {
        item.artist_id: (item.explicit_source or item.source or "explicit")
        for item in visible_preferences
        if item.explicit_selected
    }
    preferred_artist_ids = explicit_artist_ids | {
        artist_id for artist_id, weight in effective_preferences.items() if weight > 0
    }

    candidates = _load_candidates(db, preferred_artist_ids=preferred_artist_ids)
    if not candidates:
        result = RecommendationResult(items=[], personalization_active=False)
        _set_cached(user_id, requested_limit, result)
        return result

    scored = _score_candidates(
        db,
        candidates=candidates,
        user_id=user_id,
        artist_preferences=effective_preferences,
        explicit_artist_ids=explicit_artist_ids,
        explicit_artist_sources=explicit_artist_sources,
        hidden_artist_ids={item.artist_id for item in preferences if item.is_hidden},
        personalization_active=personalization_active,
    )
    shares = {
        "selected": RECOMMENDATION_CONFIG.selected_share,
        "similar": RECOMMENDATION_CONFIG.similar_share,
        "genre": RECOMMENDATION_CONFIG.genre_share,
        "popular": RECOMMENDATION_CONFIG.popular_share,
        "exploration": RECOMMENDATION_CONFIG.exploration_share,
    }
    rotation_key = f"{datetime.now(timezone.utc).date().isoformat()}:{user_id}"
    mixed = choose_weighted_mix(
        scored,
        limit=requested_limit,
        shares=shares if personalization_active else {"exploration": 0.55, "popular": 0.45},
        rotation_key=rotation_key,
    )
    result = RecommendationResult(
        items=[
            RecommendationTrack(
                track=track_to_read(item.item),
                recommendation_type=item.recommendation_type,
                reason=item.reason,
                algorithm_version=RECOMMENDATION_CONFIG.algorithm_version,
            )
            for item in mixed
        ],
        personalization_active=personalization_active,
    )
    _set_cached(user_id, requested_limit, result)
    return result


def _load_candidates(db: Session, *, preferred_artist_ids: set[int]) -> list[Track]:
    """Build a bounded global + user-aware pool without per-artist queries."""

    candidate_limit = max(50, int(RECOMMENDATION_CONFIG.candidate_limit))
    global_stmt = with_artists(
        filtered_feed_stmt()
        .order_by(
            Track.popularity_score.desc(),
            Track.quality_score.desc(),
            Track.id.desc(),
        )
        .limit(candidate_limit)
    )
    pools = [list(db.execute(global_stmt).scalars().unique().all())]

    if preferred_artist_ids:
        # Round-robin the best tracks per preferred artist. Ordering by the
        # window rank guarantees that every selected artist contributes before
        # a prolific artist can consume the bounded pool.
        ranked = (
            select(
                TrackArtist.track_id.label("track_id"),
                func.row_number()
                .over(
                    partition_by=TrackArtist.artist_id,
                    order_by=(
                        Track.popularity_score.desc(),
                        Track.quality_score.desc(),
                        Track.id.desc(),
                    ),
                )
                .label("artist_rank"),
            )
            .join(Track, Track.id == TrackArtist.track_id)
            .where(
                TrackArtist.artist_id.in_(preferred_artist_ids),
                Track.quality_score >= 60,
                Track.needs_review.is_(False),
                Track.is_playable.is_(True),
            )
            .subquery("preferred_artist_tracks")
        )
        preferred_track_ids = (
            select(ranked.c.track_id)
            .where(ranked.c.artist_rank <= 12)
            .order_by(ranked.c.artist_rank.asc(), ranked.c.track_id.desc())
            .limit(max(len(preferred_artist_ids), candidate_limit))
        )
        preferred_stmt = with_artists(
            filtered_feed_stmt()
            .where(Track.id.in_(preferred_track_ids))
            .order_by(Track.popularity_score.desc(), Track.quality_score.desc(), Track.id.desc())
        )
        pools.append(list(db.execute(preferred_stmt).scalars().unique().all()))

        raw_genres = db.execute(
            select(Track.genre)
            .join(TrackArtist, TrackArtist.track_id == Track.id)
            .where(TrackArtist.artist_id.in_(preferred_artist_ids))
            .distinct()
        ).scalars().all()
        preferred_genres = {str(value) for value in raw_genres if canonical_genre(value)}
        if preferred_genres:
            genre_stmt = with_artists(
                filtered_feed_stmt()
                .where(Track.genre.in_(preferred_genres))
                .order_by(Track.popularity_score.desc(), Track.quality_score.desc(), Track.id.desc())
                .limit(max(100, candidate_limit // 2))
            )
            pools.append(list(db.execute(genre_stmt).scalars().unique().all()))

    unique: dict[int, Track] = {}
    for pool in pools:
        for track in pool:
            if track.id not in unique and is_music_track(track):
                unique[track.id] = track
    return list(unique.values())


def _effective_artist_preferences(items: list[UserArtistPreference]) -> dict[int, float]:
    now = datetime.utcnow()
    result: dict[int, float] = {}
    for item in items:
        updated_at = item.updated_at or item.created_at or now
        age_days = max(0.0, (now - updated_at).total_seconds() / 86_400)
        decay_period = max(1.0, RECOMMENDATION_CONFIG.decay_period_days)
        behavior = float(item.behavior_weight or 0.0) * exp(-age_days / decay_period)
        total = float(item.explicit_weight or 0.0) + behavior
        result[item.artist_id] = max(
            RECOMMENDATION_CONFIG.minimum_preference_weight,
            min(RECOMMENDATION_CONFIG.maximum_preference_weight, total),
        )
    return result


def _score_candidates(
    db: Session,
    *,
    candidates: list[Track],
    user_id: int,
    artist_preferences: dict[int, float],
    explicit_artist_ids: set[int],
    explicit_artist_sources: dict[int, str] | None = None,
    hidden_artist_ids: set[int],
    personalization_active: bool,
) -> list[ScoredRecommendation[Track]]:
    artist_genres: dict[int, set[str]] = defaultdict(set)
    artist_popularity_values: dict[int, list[float]] = defaultdict(list)
    collaborations: dict[int, set[int]] = defaultdict(set)
    track_artist_ids: dict[int, list[int]] = {}
    artist_names: dict[int, str] = {}
    explicit_artist_sources = explicit_artist_sources or {}

    for track in candidates:
        artist_ids = _track_artist_ids(track)
        track_artist_ids[track.id] = artist_ids
        for link in track.artist_links:
            if link.artist_id and link.artist is not None:
                artist_names[int(link.artist_id)] = str(link.artist.name or "").strip()
        genre = canonical_genre(track.genre)
        for artist_id in artist_ids:
            if genre:
                artist_genres[artist_id].add(genre)
            artist_popularity_values[artist_id].append(max(0.0, float(track.popularity_score or 0.0)))
        for artist_id in artist_ids:
            collaborations[artist_id].update(other for other in artist_ids if other != artist_id)

    positive_preferences = {artist_id: value for artist_id, value in artist_preferences.items() if value > 0}
    missing_artist_name_ids = set(positive_preferences) - set(artist_names)
    if missing_artist_name_ids:
        artist_names.update(
            {
                int(artist_id): str(name or "").strip()
                for artist_id, name in db.execute(
                    select(Artist.id, Artist.name).where(Artist.id.in_(missing_artist_name_ids))
                ).all()
            }
        )
    max_preference = max(positive_preferences.values(), default=1.0)
    normalized_preferences = {
        artist_id: min(1.0, weight / max_preference)
        for artist_id, weight in positive_preferences.items()
    }
    negative_preferences = {
        artist_id: min(
            1.0,
            abs(weight) / max(0.001, RECOMMENDATION_CONFIG.negative_preference_scale),
        )
        for artist_id, weight in artist_preferences.items()
        if weight < 0
    }
    preferred_genres: dict[str, float] = defaultdict(float)
    for artist_id, preference in normalized_preferences.items():
        for genre in artist_genres.get(artist_id, set()):
            preferred_genres[genre] += preference
    max_genre_preference = max(preferred_genres.values(), default=1.0)
    normalized_genres = {
        genre: min(1.0, weight / max_genre_preference)
        for genre, weight in preferred_genres.items()
    }
    similar_artists = _similar_artist_scores(
        artist_genres=artist_genres,
        collaborations=collaborations,
        artist_popularity_values=artist_popularity_values,
        preferred_artist_ids=set(positive_preferences),
    )
    listened_counts, recent_listened_ids, quick_skip_counts = _history_signals(db, user_id=user_id)

    popular_values = [max(0.0, float(track.popularity_score or 0.0)) for track in candidates]
    min_popularity = min(popular_values, default=0.0)
    popularity_range = max(popular_values, default=0.0) - min_popularity
    scored: list[ScoredRecommendation[Track]] = []

    for track in candidates:
        artist_ids = track_artist_ids.get(track.id, [])
        primary_artist_id = artist_ids[0] if artist_ids else None
        if any(artist_id in hidden_artist_ids for artist_id in artist_ids):
            continue
        artist_signal = max((normalized_preferences.get(item, 0.0) for item in artist_ids), default=0.0)
        negative_artist_signal = max((negative_preferences.get(item, 0.0) for item in artist_ids), default=0.0)
        genre_key = canonical_genre(track.genre) or "unknown"
        genre_signal = normalized_genres.get(genre_key, 0.0)
        similar_match = max(
            (similar_artists[item] for item in artist_ids if item in similar_artists),
            key=lambda item: item.score,
            default=None,
        )
        similar_signal = similar_match.score if similar_match is not None else 0.0
        popularity_signal = (
            (max(0.0, float(track.popularity_score or 0.0)) - min_popularity) / popularity_range
            if popularity_range > 0
            else 0.0
        )
        exploration_signal = _stable_fraction(f"{user_id}", str(track.id))
        quality_signal = max(0.0, min(1.0, float(track.quality_score or 0.0) / 100.0))

        score = (
            artist_signal * RECOMMENDATION_CONFIG.artist_score_weight
            + genre_signal * RECOMMENDATION_CONFIG.genre_score_weight
            + similar_signal * RECOMMENDATION_CONFIG.similar_score_weight
            + popularity_signal * RECOMMENDATION_CONFIG.popularity_score_weight
            + exploration_signal * RECOMMENDATION_CONFIG.exploration_score_weight
            + quality_signal * 0.05
            - negative_artist_signal * RECOMMENDATION_CONFIG.negative_artist_score_weight
        )
        play_count = listened_counts.get(track.id, 0)
        repetition_penalty = min(0.30, play_count * 0.035)
        if track.id in recent_listened_ids:
            repetition_penalty += 0.10
        skip_penalty = min(0.35, quick_skip_counts.get(track.id, 0) * 0.12)
        score -= repetition_penalty + skip_penalty

        recommendation_type, reason = _recommendation_label(
            personalization_active=personalization_active,
            artist_ids=artist_ids,
            explicit_artist_ids=explicit_artist_ids,
            explicit_artist_sources=explicit_artist_sources,
            artist_names=artist_names,
            artist_preferences=normalized_preferences,
            artist_signal=artist_signal,
            similar_signal=similar_signal,
            similar_reference_artist_id=(
                similar_match.preferred_artist_id if similar_match is not None else None
            ),
            genre_signal=genre_signal,
            popularity_signal=popularity_signal,
        )
        scored.append(
            ScoredRecommendation(
                item=track,
                stable_key=str(track.id),
                artist_id=primary_artist_id,
                genre_key=genre_key,
                recommendation_type=recommendation_type,
                reason=reason,
                score=score,
            )
        )
    return scored


def _similar_artist_scores(
    *,
    artist_genres: dict[int, set[str]],
    collaborations: dict[int, set[int]],
    artist_popularity_values: dict[int, list[float]],
    preferred_artist_ids: set[int],
) -> dict[int, _SimilarArtistMatch]:
    scores: dict[int, _SimilarArtistMatch] = {}
    popularity = {
        artist_id: sum(values) / len(values)
        for artist_id, values in artist_popularity_values.items()
        if values
    }
    maximum_popularity = max(popularity.values(), default=0.0)

    for candidate_id in set(artist_genres) | set(artist_popularity_values):
        candidate_genres = artist_genres.get(candidate_id, set())
        if candidate_id in preferred_artist_ids:
            continue
        best = 0.0
        best_preferred_id: int | None = None
        for preferred_id in sorted(preferred_artist_ids):
            components: list[tuple[float, float]] = []
            preferred_genres = artist_genres.get(preferred_id, set())
            if candidate_genres and preferred_genres:
                overlap = len(candidate_genres & preferred_genres) / len(candidate_genres | preferred_genres)
                components.append((0.80, overlap))
            if preferred_id in collaborations.get(candidate_id, set()):
                components.append((0.15, 1.0))
            if maximum_popularity > 0 and candidate_id in popularity and preferred_id in popularity:
                compatibility = 1.0 - abs(popularity[candidate_id] - popularity[preferred_id]) / maximum_popularity
                components.append((0.05, max(0.0, compatibility)))
            if not components:
                continue
            available_weight = sum(weight for weight, _ in components)
            similarity = sum(weight * value for weight, value in components) / available_weight
            if similarity > best:
                best = similarity
                best_preferred_id = preferred_id
        if best > 0 and best_preferred_id is not None:
            scores[candidate_id] = _SimilarArtistMatch(
                score=min(1.0, best),
                preferred_artist_id=best_preferred_id,
            )
    return scores


def _history_signals(db: Session, *, user_id: int) -> tuple[dict[int, int], set[int], dict[int, int]]:
    scope = f"account:{int(user_id)}"
    substantial = or_(
        ListeningHistory.completed.is_(True),
        ListeningHistory.completion_ratio >= RECOMMENDATION_CONFIG.substantial_ratio,
    )
    legacy_play_count = case(
        (ListeningHistory.play_count > 0, ListeningHistory.play_count),
        else_=1,
    )
    play_value = case(
        (ListeningHistory.event_id.is_(None), legacy_play_count),
        (substantial, 1),
        else_=0,
    )
    listened = {
        int(track_id): int(count or 0)
        for track_id, count in db.execute(
            select(ListeningHistory.track_id, func.sum(play_value))
            .where(ListeningHistory.user_id == scope)
            .group_by(ListeningHistory.track_id)
        ).all()
        if int(count or 0) > 0
    }
    quick_skips = {
        int(track_id): int(count or 0)
        for track_id, count in db.execute(
            select(ListeningHistory.track_id, func.count(ListeningHistory.id))
            .where(
                ListeningHistory.user_id == scope,
                ListeningHistory.event_id.is_not(None),
                ListeningHistory.skipped.is_(True),
            )
            .group_by(ListeningHistory.track_id)
        ).all()
    }
    recency = case(
        (ListeningHistory.event_id.is_(None), ListeningHistory.played_at),
        else_=ListeningHistory.created_at,
    )
    recent_activity = func.max(recency).label("recent_activity")
    recent_ids = {
        int(track_id)
        for track_id, _ in db.execute(
            select(ListeningHistory.track_id, recent_activity)
            .where(
                ListeningHistory.user_id == scope,
                or_(ListeningHistory.event_id.is_(None), substantial),
            )
            .group_by(ListeningHistory.track_id)
            .order_by(recent_activity.desc())
            .limit(20)
        ).all()
    }
    return listened, recent_ids, quick_skips


def _recommendation_label(
    *,
    personalization_active: bool,
    artist_ids: list[int],
    explicit_artist_ids: set[int],
    explicit_artist_sources: dict[int, str],
    artist_names: dict[int, str],
    artist_preferences: dict[int, float],
    artist_signal: float,
    similar_signal: float,
    similar_reference_artist_id: int | None,
    genre_signal: float,
    popularity_signal: float,
) -> tuple[str, str]:
    if not personalization_active:
        if popularity_signal >= 0.45:
            return "popular", "Популярно среди слушателей"
        return "exploration", "Откройте для себя что-то новое"
    explicit_artist_id = next(
        (artist_id for artist_id in artist_ids if artist_id in explicit_artist_ids),
        None,
    )
    if explicit_artist_id is not None:
        artist_name = artist_names.get(explicit_artist_id) or "выбранного артиста"
        explicit_source = explicit_artist_sources.get(explicit_artist_id, "explicit")
        if explicit_source == "onboarding":
            return "selected", f"От {artist_name} - выбран вами при регистрации"
        if explicit_source == "settings":
            return "selected", f"От {artist_name} - выбран вами в настройках"
        return "selected", f"От {artist_name} - выбран вами в музыкальном вкусе"
    if artist_signal > 0:
        preferred_artist_id = max(
            artist_ids,
            key=lambda artist_id: artist_preferences.get(artist_id, 0.0),
            default=None,
        )
        artist_name = artist_names.get(preferred_artist_id or 0)
        if artist_name:
            return "selected", f"От {artist_name} - на основе ваших предпочтений"
        return "selected", "На основе вашей истории прослушиваний"
    if similar_signal > 0:
        reference_artist_name = artist_names.get(similar_reference_artist_id or 0)
        if reference_artist_name:
            return "similar", f"Похоже на {reference_artist_name} - по вашим предпочтениям"
        return "similar", "Похоже на любимых артистов"
    if genre_signal > 0:
        return "genre", "В жанрах, которые вам нравятся"
    if popularity_signal >= 0.45:
        return "popular", "Популярно и подходит вашему вкусу"
    return "exploration", "Новая музыка для расширения вкуса"


def _track_artist_ids(track: Track) -> list[int]:
    links = sorted(track.artist_links, key=lambda item: 0 if item.role == "main" else 1)
    return [int(link.artist_id) for link in links if link.artist_id]


def canonical_genre(value: str | None) -> str:
    cleaned = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    if cleaned in _INVALID_GENRES or cleaned.startswith(("http://", "https://", "www.")):
        return ""
    if len(cleaned) > 64:
        return ""
    return cleaned


def _stable_fraction(namespace: str, stable_key: str) -> float:
    digest = blake2b(f"{namespace}:{stable_key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float((1 << 64) - 1)


def _get_cached(user_id: int, limit: int) -> RecommendationResult | None:
    with _cache_lock:
        now = monotonic()
        for cache_user_id in [key for key, value in _cache.items() if value.expires_at <= now]:
            _cache.pop(cache_user_id, None)
        entry = _cache.get(int(user_id))
        if not entry or len(entry.result.items) < limit:
            return None
        _cache.move_to_end(int(user_id))
        return RecommendationResult(
            items=[item.model_copy(deep=True) for item in entry.result.items[:limit]],
            personalization_active=entry.result.personalization_active,
        )


def _set_cached(user_id: int, limit: int, result: RecommendationResult) -> None:
    del limit
    with _cache_lock:
        _cache[int(user_id)] = _CacheEntry(
            expires_at=monotonic() + max(1, RECOMMENDATION_CONFIG.cache_ttl_seconds),
            result=RecommendationResult(
                items=[item.model_copy(deep=True) for item in result.items],
                personalization_active=result.personalization_active,
            ),
        )
        _cache.move_to_end(int(user_id))
        maximum = max(1, int(RECOMMENDATION_CONFIG.cache_max_entries))
        while len(_cache) > maximum:
            _cache.popitem(last=False)
