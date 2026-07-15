from __future__ import annotations

import json
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import exp, log1p, sqrt
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

_GENERIC_TAGS = _INVALID_GENRES | {
    "audio",
    "official",
    "provider",
    "track",
    "untitled",
}

_GENRE_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("drum and bass", "drum-and-bass", "electronic"),
    ("drum bass", "drum-and-bass", "electronic"),
    ("dnb", "drum-and-bass", "electronic"),
    ("hip hop rap", "hip-hop", "hip-hop"),
    ("hip hop", "hip-hop", "hip-hop"),
    ("rap hip hop", "hip-hop", "hip-hop"),
    ("cloud rap", "cloud-rap", "hip-hop"),
    ("alternative rock", "alternative-rock", "rock"),
    ("alt rock", "alternative-rock", "rock"),
    ("r b soul", "r-and-b", "r-and-b"),
    ("rhythm and blues", "r-and-b", "r-and-b"),
    ("dance edm", "dance-edm", "electronic"),
    ("edm", "dance-edm", "electronic"),
    ("electro", "electronic", "electronic"),
    ("electronic", "electronic", "electronic"),
    ("ambient", "ambient", "electronic"),
    ("house", "house", "electronic"),
    ("techno", "techno", "electronic"),
    ("phonk", "phonk", "hip-hop"),
    ("trap", "trap", "hip-hop"),
    ("drill", "drill", "hip-hop"),
    ("grime", "grime", "hip-hop"),
    ("rap", "hip-hop", "hip-hop"),
    ("indie", "indie", "alternative"),
    ("alternative", "alternative", "alternative"),
    ("rock", "rock", "rock"),
    ("pop", "pop", "pop"),
    ("country", "country", "country"),
    ("classical", "classical", "classical"),
    ("jazz", "jazz", "jazz"),
    ("soundtrack", "soundtrack", "soundtrack"),
)


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
    collaborative_score: float


@dataclass(frozen=True)
class _ArtistSimilarityFeatures:
    genres: frozenset[str]
    genre_families: frozenset[str]
    tags: frozenset[str]
    listeners: frozenset[str]
    preference_users: frozenset[int]
    followers_count: int
    profile_quality: float


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
    available_types = {item.recommendation_type for item in scored}
    if personalization_active and "similar" not in available_types:
        # Sparse metadata should produce more tracks from known-good anchors,
        # not a page of weak matches mislabeled as similar artists.
        missing_similarity_share = shares["similar"]
        shares["similar"] = 0.0
        shares["selected"] += missing_similarity_share * 0.65
        if "genre" in available_types:
            shares["genre"] += missing_similarity_share * 0.35
        else:
            shares["exploration"] += missing_similarity_share * 0.35
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
        preferred_genre_features: set[str] = set()
        for raw_genre in raw_genres:
            detailed, families = _genre_features(raw_genre)
            preferred_genre_features.update(detailed | families)

        # Discover spelling variants (Hip-Hop/Rap vs Hip-hop & Rap, EDM vs
        # Dance & EDM, and so on) before the candidate pool is materialized.
        # Only distinct genre strings are loaded, never the full track table.
        available_raw_genres = db.execute(
            select(Track.genre)
            .where(
                Track.genre.is_not(None),
                Track.is_playable.is_(True),
                Track.needs_review.is_(False),
            )
            .distinct()
        ).scalars().all()
        matching_raw_genres = {
            str(raw_genre)
            for raw_genre in available_raw_genres
            if (_genre_features(raw_genre)[0] | _genre_features(raw_genre)[1])
            & preferred_genre_features
        }
        if matching_raw_genres:
            genre_stmt = with_artists(
                filtered_feed_stmt()
                .where(Track.genre.in_(matching_raw_genres))
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
    track_artist_ids: dict[int, list[int]] = {}
    artist_names: dict[int, str] = {}
    candidate_artist_ids: set[int] = set()
    explicit_artist_sources = explicit_artist_sources or {}

    for track in candidates:
        artist_ids = _track_artist_ids(track)
        track_artist_ids[track.id] = artist_ids
        candidate_artist_ids.update(artist_ids)
        for link in track.artist_links:
            if link.artist_id and link.artist is not None:
                artist_names[int(link.artist_id)] = str(link.artist.name or "").strip()

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
    similarity_features, collaborations = _load_artist_similarity_features(
        db,
        artist_ids=candidate_artist_ids | set(positive_preferences),
        current_user_id=user_id,
    )
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
        profile = similarity_features.get(artist_id)
        if profile is None:
            continue
        for genre in profile.genres | profile.genre_families:
            preferred_genres[genre] += preference
    max_genre_preference = max(preferred_genres.values(), default=1.0)
    normalized_genres = {
        genre: min(1.0, weight / max_genre_preference)
        for genre, weight in preferred_genres.items()
    }
    similar_artists = _similar_artist_scores(
        features=similarity_features,
        collaborations=collaborations,
        preferred_artist_weights=normalized_preferences,
    )
    listened_counts, recent_listened_ids, quick_skip_counts = _history_signals(db, user_id=user_id)

    popular_values = [max(0.0, float(track.popularity_score or 0.0)) for track in candidates]
    min_popularity = min(popular_values, default=0.0)
    popularity_range = max(popular_values, default=0.0) - min_popularity
    maximum_log_followers = max(
        (log1p(profile.followers_count) for profile in similarity_features.values()),
        default=0.0,
    )
    scored: list[ScoredRecommendation[Track]] = []

    for track in candidates:
        artist_ids = track_artist_ids.get(track.id, [])
        inferred_artist_ids = _inferred_preferred_artist_ids(
            track,
            artist_names=artist_names,
            preferred_artist_ids=set(positive_preferences),
        )
        scoring_artist_ids = list(dict.fromkeys([*inferred_artist_ids, *artist_ids]))
        primary_artist_id = scoring_artist_ids[0] if scoring_artist_ids else None
        if any(artist_id in hidden_artist_ids for artist_id in scoring_artist_ids):
            continue
        artist_signal = max(
            (normalized_preferences.get(item, 0.0) for item in scoring_artist_ids),
            default=0.0,
        )
        negative_artist_signal = max(
            (negative_preferences.get(item, 0.0) for item in scoring_artist_ids),
            default=0.0,
        )
        track_genres, track_genre_families = _genre_features(track.genre)
        genre_keys = track_genres | track_genre_families
        genre_key = _primary_genre_key(track_genres, track_genre_families)
        genre_signal = max((normalized_genres.get(item, 0.0) for item in genre_keys), default=0.0)
        similar_match = max(
            (similar_artists[item] for item in artist_ids if item in similar_artists),
            key=lambda item: item.score,
            default=None,
        )
        similar_signal = similar_match.score if similar_match is not None else 0.0
        collaborative_signal = (
            similar_match.collaborative_score if similar_match is not None else 0.0
        )
        track_popularity_signal = (
            (max(0.0, float(track.popularity_score or 0.0)) - min_popularity) / popularity_range
            if popularity_range > 0
            else 0.0
        )
        artist_reach_signal = max(
            (
                log1p(similarity_features[item].followers_count) / maximum_log_followers
                for item in scoring_artist_ids
                if item in similarity_features and maximum_log_followers > 0
            ),
            default=0.0,
        )
        artist_profile_signal = max(
            (
                similarity_features[item].profile_quality
                for item in scoring_artist_ids
                if item in similarity_features
            ),
            default=0.0,
        )
        popularity_signal = (
            track_popularity_signal * 0.55
            + artist_reach_signal * 0.30
            + artist_profile_signal * 0.15
        )
        exploration_signal = _stable_fraction(f"{user_id}", str(track.id))
        quality_signal = max(0.0, min(1.0, float(track.quality_score or 0.0) / 100.0))
        freshness_signal = _freshness_score(track.created_at)

        score = (
            artist_signal * RECOMMENDATION_CONFIG.artist_score_weight
            + genre_signal * RECOMMENDATION_CONFIG.genre_score_weight
            + similar_signal * RECOMMENDATION_CONFIG.similar_score_weight
            + freshness_signal * RECOMMENDATION_CONFIG.freshness_score_weight
            + popularity_signal * RECOMMENDATION_CONFIG.popularity_score_weight
            + collaborative_signal * RECOMMENDATION_CONFIG.collaborative_score_weight
            + exploration_signal * RECOMMENDATION_CONFIG.exploration_score_weight
            - negative_artist_signal * RECOMMENDATION_CONFIG.negative_artist_score_weight
        )
        # Quality is a confidence multiplier rather than an extra additive
        # weight, keeping the documented score weights normalized.
        score *= 0.85 + quality_signal * 0.15
        play_count = listened_counts.get(track.id, 0)
        repetition_penalty = min(0.30, play_count * 0.035)
        if track.id in recent_listened_ids:
            repetition_penalty += 0.10
        skip_penalty = min(0.35, quick_skip_counts.get(track.id, 0) * 0.12)
        score -= repetition_penalty + skip_penalty

        recommendation_type, reason = _recommendation_label(
            personalization_active=personalization_active,
            artist_ids=scoring_artist_ids,
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
            genre_label=_display_genre(track.genre),
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


def _load_artist_similarity_features(
    db: Session,
    *,
    artist_ids: set[int],
    current_user_id: int,
) -> tuple[dict[int, _ArtistSimilarityFeatures], dict[int, set[int]]]:
    """Load catalog, audience and co-preference evidence in bounded batches."""

    resolved_ids = {int(item) for item in artist_ids if int(item) > 0}
    if not resolved_ids:
        return {}, {}

    genres: dict[int, set[str]] = defaultdict(set)
    genre_families: dict[int, set[str]] = defaultdict(set)
    tags: dict[int, set[str]] = defaultdict(set)
    listeners: dict[int, set[str]] = defaultdict(set)
    preference_users: dict[int, set[int]] = defaultdict(set)
    followers: dict[int, int] = defaultdict(int)
    profile_quality: dict[int, float] = defaultdict(float)

    for (
        artist_id,
        genres_json,
        followers_count,
        source_verified,
        is_canonical,
        confidence_score,
        needs_review,
    ) in db.execute(
        select(
            Artist.id,
            Artist.genres_json,
            Artist.source_followers_count,
            Artist.source_verified,
            Artist.is_canonical,
            Artist.confidence_score,
            Artist.needs_review,
        ).where(Artist.id.in_(resolved_ids))
    ).all():
        followers[int(artist_id)] = max(0, int(followers_count or 0))
        confidence = max(0.0, min(1.0, float(confidence_score or 0.0)))
        profile_quality[int(artist_id)] = 0.0 if needs_review else min(
            1.0,
            confidence * 0.35
            + float(bool(is_canonical)) * 0.45
            + float(bool(source_verified)) * 0.20,
        )
        for raw_genre in _json_string_list(genres_json):
            detailed, families = _genre_features(raw_genre)
            genres[int(artist_id)].update(detailed)
            genre_families[int(artist_id)].update(families)

    relevant_track_ids: set[int] = set()
    for artist_id, track_id, raw_genre, tags_json in db.execute(
        select(TrackArtist.artist_id, Track.id, Track.genre, Track.tags_json)
        .join(Track, Track.id == TrackArtist.track_id)
        .where(
            TrackArtist.artist_id.in_(resolved_ids),
            Track.is_playable.is_(True),
            Track.needs_review.is_(False),
        )
    ).all():
        normalized_artist_id = int(artist_id)
        relevant_track_ids.add(int(track_id))
        detailed, families = _genre_features(raw_genre)
        genres[normalized_artist_id].update(detailed)
        genre_families[normalized_artist_id].update(families)
        tags[normalized_artist_id].update(_tag_features(tags_json))

    collaborations: dict[int, set[int]] = defaultdict(set)
    if relevant_track_ids:
        artists_by_track: dict[int, set[int]] = defaultdict(set)
        ordered_track_ids = sorted(relevant_track_ids)
        for offset in range(0, len(ordered_track_ids), 500):
            track_id_batch = ordered_track_ids[offset : offset + 500]
            for track_id, artist_id in db.execute(
                select(TrackArtist.track_id, TrackArtist.artist_id).where(
                    TrackArtist.track_id.in_(track_id_batch)
                )
            ).all():
                artists_by_track[int(track_id)].add(int(artist_id))
        for linked_artists in artists_by_track.values():
            if len(linked_artists) < 2:
                continue
            for artist_id in linked_artists & resolved_ids:
                collaborations[artist_id].update(linked_artists - {artist_id})

    substantial = or_(
        ListeningHistory.event_id.is_(None),
        ListeningHistory.completed.is_(True),
        ListeningHistory.completion_ratio >= RECOMMENDATION_CONFIG.substantial_ratio,
    )
    for artist_id, listener_id in db.execute(
        select(ListeningHistory.artist_id, ListeningHistory.user_id)
        .where(
            ListeningHistory.artist_id.in_(resolved_ids),
            ListeningHistory.skipped.is_(False),
            substantial,
        )
        .distinct()
    ).all():
        if artist_id is not None and listener_id:
            listeners[int(artist_id)].add(str(listener_id))

    # Older history rows did not store artist_id. Resolve them through the
    # existing track relation rather than discarding useful collaborative data.
    for artist_id, listener_id in db.execute(
        select(TrackArtist.artist_id, ListeningHistory.user_id)
        .join(ListeningHistory, ListeningHistory.track_id == TrackArtist.track_id)
        .where(
            ListeningHistory.artist_id.is_(None),
            TrackArtist.artist_id.in_(resolved_ids),
            ListeningHistory.skipped.is_(False),
            substantial,
        )
        .distinct()
    ).all():
        if listener_id:
            listeners[int(artist_id)].add(str(listener_id))

    for artist_id, preference_user_id in db.execute(
        select(UserArtistPreference.artist_id, UserArtistPreference.user_id)
        .where(
            UserArtistPreference.artist_id.in_(resolved_ids),
            UserArtistPreference.user_id != int(current_user_id),
            UserArtistPreference.is_hidden.is_(False),
            or_(
                UserArtistPreference.explicit_selected.is_(True),
                UserArtistPreference.weight > 0,
            ),
        )
        .distinct()
    ).all():
        preference_users[int(artist_id)].add(int(preference_user_id))

    features = {
        artist_id: _ArtistSimilarityFeatures(
            genres=frozenset(genres.get(artist_id, set())),
            genre_families=frozenset(genre_families.get(artist_id, set())),
            tags=frozenset(tags.get(artist_id, set())),
            listeners=frozenset(listeners.get(artist_id, set())),
            preference_users=frozenset(preference_users.get(artist_id, set())),
            followers_count=followers.get(artist_id, 0),
            profile_quality=profile_quality.get(artist_id, 0.0),
        )
        for artist_id in resolved_ids
    }
    return features, collaborations


def _similar_artist_scores(
    *,
    features: dict[int, _ArtistSimilarityFeatures],
    collaborations: dict[int, set[int]],
    preferred_artist_weights: dict[int, float],
) -> dict[int, _SimilarArtistMatch]:
    """Return only similarities supported by more than provider popularity.

    Missing components intentionally contribute zero. The previous algorithm
    divided by the sum of whichever components happened to exist, so a lone
    popularity match could become a 100% similarity claim.
    """

    scores: dict[int, _SimilarArtistMatch] = {}
    preferred_ids = set(preferred_artist_weights)
    minimum_score = max(0.0, min(1.0, RECOMMENDATION_CONFIG.similarity_min_score))

    for candidate_id, candidate in features.items():
        if candidate_id in preferred_ids:
            continue
        best_match: _SimilarArtistMatch | None = None
        for preferred_id, raw_anchor_weight in sorted(preferred_artist_weights.items()):
            preferred = features.get(preferred_id)
            if preferred is None:
                continue

            genre_similarity = _jaccard(candidate.genres, preferred.genres)
            family_similarity = _jaccard(
                candidate.genre_families,
                preferred.genre_families,
            )
            tag_similarity = _cosine_set_similarity(candidate.tags, preferred.tags)
            audience_similarity = _shrunk_set_similarity(
                candidate.listeners,
                preferred.listeners,
            )
            co_preference_similarity = _shrunk_set_similarity(
                candidate.preference_users,
                preferred.preference_users,
            )
            collaboration_similarity = float(
                preferred_id in collaborations.get(candidate_id, set())
            )
            popularity_similarity = _popularity_compatibility(
                candidate.followers_count,
                preferred.followers_count,
            )

            has_meaningful_evidence = any(
                value > 0
                for value in (
                    genre_similarity,
                    family_similarity,
                    tag_similarity,
                    audience_similarity,
                    co_preference_similarity,
                    collaboration_similarity,
                )
            )
            if not has_meaningful_evidence:
                continue

            similarity = (
                genre_similarity * RECOMMENDATION_CONFIG.similarity_genre_weight
                + family_similarity * RECOMMENDATION_CONFIG.similarity_genre_family_weight
                + tag_similarity * RECOMMENDATION_CONFIG.similarity_tag_weight
                + audience_similarity * RECOMMENDATION_CONFIG.similarity_audience_weight
                + co_preference_similarity
                * RECOMMENDATION_CONFIG.similarity_co_preference_weight
                + collaboration_similarity
                * RECOMMENDATION_CONFIG.similarity_collaboration_weight
                + popularity_similarity
                * RECOMMENDATION_CONFIG.similarity_popularity_weight
            )
            anchor_weight = max(0.0, min(1.0, float(raw_anchor_weight)))
            similarity *= 0.65 + anchor_weight * 0.35
            if collaboration_similarity > 0:
                similarity = max(similarity, 0.50 * (0.65 + anchor_weight * 0.35))

            collaborative_score = max(
                audience_similarity,
                co_preference_similarity,
                collaboration_similarity,
            )
            match = _SimilarArtistMatch(
                score=min(1.0, similarity),
                preferred_artist_id=preferred_id,
                collaborative_score=collaborative_score,
            )
            if match.score >= minimum_score and (
                best_match is None or match.score > best_match.score
            ):
                best_match = match
        if best_match is not None:
            scores[candidate_id] = best_match
    return scores


def _json_string_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_feature_text(value: str | None) -> str:
    cleaned = str(value or "").strip().lower().replace("ё", "е").replace("_", " ")
    return " ".join(re.sub(r"[^\w]+", " ", cleaned, flags=re.UNICODE).split())


def _genre_features(value: str | None) -> tuple[set[str], set[str]]:
    cleaned = _normalize_feature_text(value)
    if (
        not cleaned
        or cleaned in _INVALID_GENRES
        or cleaned.isdigit()
        or cleaned.startswith(("http ", "https ", "www "))
        or len(cleaned) > 64
    ):
        return set(), set()

    padded = f" {cleaned} "
    for alias, detailed, family in _GENRE_ALIASES:
        if cleaned == alias or f" {alias} " in padded:
            return {f"genre:{detailed}"}, {f"family:{family}"}
    return {f"genre:{cleaned.replace(' ', '-')}"}, set()


def _tag_features(raw: str | None) -> set[str]:
    result: set[str] = set()
    for item in _json_string_list(raw):
        cleaned = _normalize_feature_text(item)
        if (
            not cleaned
            or cleaned in _GENERIC_TAGS
            or cleaned.isdigit()
            or len(cleaned) < 3
            or len(cleaned) > 64
            or cleaned.startswith(("http ", "https ", "www "))
        ):
            continue
        result.add(cleaned)
        for token in cleaned.split():
            if len(token) >= 3 and token not in _GENERIC_TAGS and not token.isdigit():
                result.add(token)
    return result


def _inferred_preferred_artist_ids(
    track: Track,
    *,
    artist_names: dict[int, str],
    preferred_artist_ids: set[int],
) -> list[int]:
    """Recognize selected artists credited in trusted catalog text metadata.

    Some provider rows are linked to the uploader while the title/tags carry
    the canonical performing artist. Treat those tracks as direct selections
    so the UI never says "Kai Angel sounds like Kai Angel".
    """

    title = f" {_normalize_feature_text(track.title)} "
    track_tags = _tag_features(track.tags_json)
    inferred: list[int] = []
    for artist_id in sorted(preferred_artist_ids):
        raw_name = artist_names.get(artist_id, "")
        variants = {
            _normalize_feature_text(raw_name),
            _normalize_feature_text(re.split(r"\s*\(@?", raw_name, maxsplit=1)[0]),
        }
        matched = False
        for variant in {item for item in variants if item}:
            if variant in track_tags:
                matched = True
                break
            if len(variant) >= 4 and f" {variant} " in title:
                matched = True
                break
        if matched:
            inferred.append(artist_id)
    return inferred


def _primary_genre_key(genres: set[str], families: set[str]) -> str:
    if genres:
        return sorted(genres)[0]
    if families:
        return sorted(families)[0]
    return "unknown"


def _display_genre(value: str | None) -> str | None:
    raw = " ".join(str(value or "").strip().split())
    detailed, families = _genre_features(raw)
    if not raw or not (detailed or families) or len(raw) > 48:
        return None
    return raw


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _cosine_set_similarity(left: frozenset, right: frozenset) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    if overlap <= 0:
        return 0.0
    return overlap / sqrt(len(left) * len(right))


def _shrunk_set_similarity(left: frozenset, right: frozenset) -> float:
    overlap = len(left & right)
    if overlap <= 0:
        return 0.0
    # One shared listener is useful but not enough to dominate the feed.
    support = overlap / (overlap + 2.0)
    return _cosine_set_similarity(left, right) * support


def _popularity_compatibility(left_followers: int, right_followers: int) -> float:
    if left_followers <= 0 or right_followers <= 0:
        return 0.0
    distance = abs(log1p(left_followers) - log1p(right_followers))
    return exp(-distance / 3.0)


def _freshness_score(created_at: datetime | None) -> float:
    if created_at is None:
        return 0.0
    now = datetime.now(timezone.utc) if created_at.tzinfo else datetime.utcnow()
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    return exp(-age_days / 365.0)


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
    genre_label: str | None,
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
        if genre_label:
            return "genre", f"В жанре «{genre_label}» - по вашим предпочтениям"
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
