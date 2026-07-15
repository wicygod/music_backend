from __future__ import annotations

from dataclasses import dataclass

from app.config import get_float_env, get_int_env


@dataclass(frozen=True)
class RecommendationConfig:
    """Central tuning knobs for preference learning and home recommendations."""

    minimum_onboarding_artists: int = get_int_env("MUSIC_ONBOARDING_MIN_ARTISTS", 3)
    onboarding_page_size: int = get_int_env("MUSIC_ONBOARDING_PAGE_SIZE", 24)
    onboarding_min_profile_followers: int = get_int_env(
        "MUSIC_ONBOARDING_MIN_PROFILE_FOLLOWERS",
        1_000,
    )
    recommendation_limit: int = get_int_env("MUSIC_RECOMMENDATION_LIMIT", 48)
    candidate_limit: int = get_int_env("MUSIC_RECOMMENDATION_CANDIDATES", 600)
    cache_ttl_seconds: int = get_int_env("MUSIC_RECOMMENDATION_CACHE_TTL", 60)
    cache_max_entries: int = get_int_env("MUSIC_RECOMMENDATION_CACHE_MAX_ENTRIES", 2_048)
    decay_period_days: float = get_float_env("MUSIC_PREFERENCE_DECAY_DAYS", 90.0)
    minimum_preference_weight: float = get_float_env("MUSIC_PREFERENCE_MIN_WEIGHT", -20.0)
    maximum_preference_weight: float = get_float_env("MUSIC_PREFERENCE_MAX_WEIGHT", 50.0)

    onboarding_weight: float = get_float_env("MUSIC_SIGNAL_ONBOARDING", 5.0)
    completed_play_weight: float = get_float_env("MUSIC_SIGNAL_COMPLETED", 2.0)
    substantial_play_weight: float = get_float_env("MUSIC_SIGNAL_SUBSTANTIAL_PLAY", 1.5)
    repeat_play_weight: float = get_float_env("MUSIC_SIGNAL_REPEAT", 2.0)
    like_weight: float = get_float_env("MUSIC_SIGNAL_LIKE", 4.0)
    playlist_weight: float = get_float_env("MUSIC_SIGNAL_PLAYLIST", 3.0)
    follow_weight: float = get_float_env("MUSIC_SIGNAL_FOLLOW", 5.0)
    artist_view_weight: float = get_float_env("MUSIC_SIGNAL_ARTIST_VIEW", 0.5)
    quick_skip_weight: float = get_float_env("MUSIC_SIGNAL_QUICK_SKIP", -1.0)
    repeated_quick_skip_weight: float = get_float_env("MUSIC_SIGNAL_REPEATED_QUICK_SKIP", -0.5)
    quick_skip_seconds: float = get_float_env("MUSIC_QUICK_SKIP_SECONDS", 12.0)
    substantial_ratio: float = get_float_env("MUSIC_SUBSTANTIAL_LISTEN_RATIO", 0.7)
    completed_ratio: float = get_float_env("MUSIC_COMPLETED_LISTEN_RATIO", 0.9)

    selected_share: float = get_float_env("MUSIC_FEED_SELECTED_SHARE", 0.35)
    similar_share: float = get_float_env("MUSIC_FEED_SIMILAR_SHARE", 0.25)
    genre_share: float = get_float_env("MUSIC_FEED_GENRE_SHARE", 0.20)
    popular_share: float = get_float_env("MUSIC_FEED_POPULAR_SHARE", 0.10)
    exploration_share: float = get_float_env("MUSIC_FEED_EXPLORATION_SHARE", 0.10)

    artist_score_weight: float = get_float_env("MUSIC_TRACK_ARTIST_WEIGHT", 0.30)
    genre_score_weight: float = get_float_env("MUSIC_TRACK_GENRE_WEIGHT", 0.20)
    similar_score_weight: float = get_float_env("MUSIC_TRACK_SIMILAR_WEIGHT", 0.15)
    freshness_score_weight: float = get_float_env("MUSIC_TRACK_FRESHNESS_WEIGHT", 0.10)
    popularity_score_weight: float = get_float_env("MUSIC_TRACK_POPULARITY_WEIGHT", 0.10)
    collaborative_score_weight: float = get_float_env("MUSIC_TRACK_COLLABORATIVE_WEIGHT", 0.10)
    exploration_score_weight: float = get_float_env("MUSIC_TRACK_EXPLORATION_WEIGHT", 0.05)
    negative_artist_score_weight: float = get_float_env("MUSIC_TRACK_NEGATIVE_ARTIST_WEIGHT", 0.30)
    negative_preference_scale: float = get_float_env("MUSIC_NEGATIVE_PREFERENCE_SCALE", 5.0)

    algorithm_version: str = "personalized-v1"


RECOMMENDATION_CONFIG = RecommendationConfig()


SIGNAL_WEIGHTS = {
    "like": RECOMMENDATION_CONFIG.like_weight,
    "unlike": -RECOMMENDATION_CONFIG.like_weight,
    "playlist": RECOMMENDATION_CONFIG.playlist_weight,
    "playlist_remove": -RECOMMENDATION_CONFIG.playlist_weight,
    "follow": RECOMMENDATION_CONFIG.follow_weight,
    "artist_view": RECOMMENDATION_CONFIG.artist_view_weight,
    "hide": RECOMMENDATION_CONFIG.minimum_preference_weight,
}
