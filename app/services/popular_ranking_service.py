from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import ceil, log1p
from typing import Generic, Iterable, TypeVar

from app.config import get_float_env, get_int_env


T = TypeVar("T")


@dataclass(frozen=True)
class PopularRankingConfig:
    """Central tuning for the public chart.

    Provider reach and canonical-artist authority make cold start useful while
    unique listeners and qualified playback gradually take over.  Every value
    is configurable without changing the API contract.
    """

    min_artist_followers: int = get_int_env("MUSIC_POPULAR_MIN_ARTIST_FOLLOWERS", 3_000)
    strong_artist_followers: int = get_int_env("MUSIC_POPULAR_STRONG_ARTIST_FOLLOWERS", 20_000)
    min_unique_listeners: int = get_int_env("MUSIC_POPULAR_MIN_UNIQUE_LISTENERS", 3)
    repeat_cap_per_user: int = get_int_env("MUSIC_POPULAR_REPEAT_CAP_PER_USER", 5)
    provider_weight: float = get_float_env("MUSIC_POPULAR_PROVIDER_WEIGHT", 0.36)
    artist_weight: float = get_float_env("MUSIC_POPULAR_ARTIST_WEIGHT", 0.32)
    engagement_weight: float = get_float_env("MUSIC_POPULAR_ENGAGEMENT_WEIGHT", 0.26)
    quality_weight: float = get_float_env("MUSIC_POPULAR_QUALITY_WEIGHT", 0.06)
    unknown_provider_artist_weight: float = get_float_env(
        "MUSIC_POPULAR_UNKNOWN_PROVIDER_ARTIST_WEIGHT",
        0.52,
    )
    unknown_provider_engagement_weight: float = get_float_env(
        "MUSIC_POPULAR_UNKNOWN_PROVIDER_ENGAGEMENT_WEIGHT",
        0.36,
    )
    unknown_provider_quality_weight: float = get_float_env(
        "MUSIC_POPULAR_UNKNOWN_PROVIDER_QUALITY_WEIGHT",
        0.12,
    )
    min_provider_track_score: float = get_float_env("MUSIC_POPULAR_MIN_PROVIDER_SCORE", 45.0)
    variant_penalty: float = get_float_env("MUSIC_POPULAR_VARIANT_PENALTY", 18.0)


POPULAR_RANKING_CONFIG = PopularRankingConfig()
POPULAR_ALGORITHM_VERSION = "popular-v2"
PROVIDER_POPULARITY_TAG = "provider-popularity-v1"


@dataclass(frozen=True)
class PopularCandidate(Generic[T]):
    """Provider-agnostic input for the home-feed diversity ranker."""

    item: T
    stable_key: str
    artist_key: str
    genre_key: str = "unknown"
    region_key: str = "unknown"
    popularity_score: float = 0.0
    quality_score: float = 0.0
    provider_signal_reliable: bool = False
    artist_followers: int = 0
    artist_verified: bool = False
    artist_canonical: bool = False
    unique_listeners: int = 0
    capped_plays: int = 0
    recent_plays: int = 0
    detailed_plays: int = 0
    completed_plays: int = 0
    skipped_plays: int = 0
    favorite_count: int = 0
    playlist_add_count: int = 0
    low_value_variant: bool = False


def provider_popularity_score(
    *,
    view_count: object = None,
    like_count: object = None,
    repost_count: object = None,
    timestamp: object = None,
    fallback: float = 0.0,
    now_timestamp: float | None = None,
) -> tuple[float, bool]:
    """Normalize public provider counters into one stable 0..100 score.

    SoundCloud search exposes real plays, likes, reposts and upload time.  The
    previous importer discarded them and assigned every track the same score.
    Logarithmic scaling keeps a viral release from permanently flattening the
    rest of the catalogue while a velocity component gives new releases room.
    """

    views = _optional_non_negative_int(view_count)
    likes = _optional_non_negative_int(like_count)
    reposts = _optional_non_negative_int(repost_count)
    published_at = _optional_non_negative_float(timestamp)
    if not any(value is not None and value > 0 for value in (views, likes, reposts)):
        return _clamp_score(fallback), False

    components: list[tuple[float, float]] = []
    if views is not None:
        components.append((0.62, _log_score(views, 10_000_000)))
    if likes is not None:
        components.append((0.20, _log_score(likes, 500_000)))
    if reposts is not None:
        components.append((0.05, _log_score(reposts, 50_000)))
    if views is not None and published_at:
        current = float(now_timestamp) if now_timestamp is not None else datetime.now(timezone.utc).timestamp()
        age_days = max(1.0, (current - published_at) / 86_400.0)
        components.append((0.13, _log_score(views / age_days, 250_000)))

    total_weight = sum(weight for weight, _score in components)
    if total_weight <= 0:
        return _clamp_score(fallback), False
    score = sum(weight * value for weight, value in components) / total_weight
    return round(_clamp_score(score), 3), True


def artist_authority_score(candidate: PopularCandidate[object]) -> float:
    followers = max(0, int(candidate.artist_followers or 0))
    reach = _log_score(followers, 3_500_000)
    verification_bonus = 8.0 if candidate.artist_verified else 0.0
    return _clamp_score(reach + verification_bonus)


def local_engagement_score(candidate: PopularCandidate[object]) -> float:
    unique_listeners = max(0, int(candidate.unique_listeners or 0))
    capped_plays = max(0, int(candidate.capped_plays or 0))
    recent_plays = max(0, int(candidate.recent_plays or 0))
    detailed = max(0, int(candidate.detailed_plays or 0))
    completed = max(0, int(candidate.completed_plays or 0))
    skipped = max(0, int(candidate.skipped_plays or 0))
    saves = max(0, int(candidate.favorite_count or 0)) + max(
        0,
        int(candidate.playlist_add_count or 0),
    ) * 2

    listener_score = min(1.0, unique_listeners / 12.0) * 40.0
    play_score = _log_score(capped_plays, 50) * 0.15
    recent_score = _log_score(recent_plays, 30) * 0.15
    completion_score = (completed / detailed) * 15.0 if detailed else 0.0
    save_score = _log_score(saves, 12) * 0.15
    skip_penalty = (skipped / detailed) * 30.0 if detailed else 0.0
    return _clamp_score(listener_score + play_score + recent_score + completion_score + save_score - skip_penalty)


def popular_candidate_score(
    candidate: PopularCandidate[object],
    *,
    config: PopularRankingConfig = POPULAR_RANKING_CONFIG,
) -> float:
    provider = _clamp_score(candidate.popularity_score)
    artist = artist_authority_score(candidate)
    engagement = local_engagement_score(candidate)
    quality = _clamp_score(candidate.quality_score)
    if candidate.provider_signal_reliable:
        score = (
            provider * config.provider_weight
            + artist * config.artist_weight
            + engagement * config.engagement_weight
            + quality * config.quality_weight
        )
    else:
        score = (
            artist * config.unknown_provider_artist_weight
            + engagement * config.unknown_provider_engagement_weight
            + quality * config.unknown_provider_quality_weight
        )
    if candidate.low_value_variant:
        score -= config.variant_penalty
    return round(_clamp_score(score), 3)


def is_popular_candidate_eligible(
    candidate: PopularCandidate[object],
    *,
    config: PopularRankingConfig = POPULAR_RANKING_CONFIG,
) -> bool:
    """Apply a conservative evidence gate before diversity reranking."""

    if not candidate.artist_canonical:
        return False
    followers = max(0, int(candidate.artist_followers or 0))
    locally_proven = candidate.unique_listeners >= config.min_unique_listeners
    saved_by_users = candidate.favorite_count + candidate.playlist_add_count >= 2
    authoritative_artist = (
        candidate.artist_verified
        or followers >= config.min_artist_followers
        or locally_proven
    )
    if not authoritative_artist:
        return False

    provider_proven = (
        candidate.provider_signal_reliable
        and candidate.popularity_score >= config.min_provider_track_score
    )
    safe_cold_start = (
        not candidate.provider_signal_reliable
        and followers >= config.strong_artist_followers
        and candidate.quality_score >= 80
    )
    if candidate.low_value_variant and not (
        candidate.popularity_score >= 78
        or candidate.unique_listeners >= config.min_unique_listeners + 2
    ):
        return False
    return provider_proven or locally_proven or saved_by_users or safe_cold_start


def rank_popular_candidates(
    candidates: Iterable[PopularCandidate[T]],
    *,
    limit: int,
    rotation_key: str,
    head_size: int = 24,
    head_artist_cap: int = 2,
    artist_gap: int = 3,
) -> list[PopularCandidate[T]]:
    """Return a relevant but deliberately varied, deterministic popular feed.

    The order remains stable for the supplied rotation key. Callers normally use
    a key made from the UTC date and user id, so the shelf rotates once a day
    without jumping around on every refresh.
    """

    requested_limit = max(0, int(limit))
    if requested_limit == 0:
        return []

    unique: list[PopularCandidate[T]] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        stable_key = str(candidate.stable_key or "").strip()
        if not stable_key or stable_key in seen_keys:
            continue
        seen_keys.add(stable_key)
        unique.append(candidate)

    if not unique:
        return []

    requested_limit = min(requested_limit, len(unique))
    head_size = min(max(0, int(head_size)), requested_limit)
    artist_gap = max(0, int(artist_gap))
    head_artist_cap = max(1, int(head_artist_cap))
    overall_artist_cap = min(8, max(2, ceil(requested_limit * 0.05)))
    available_artists = {_clean_key(item.artist_key) for item in unique}
    target_head_artists = min(len(available_artists), ceil(head_size / head_artist_cap))
    useful_genres = {
        _clean_key(item.genre_key)
        for item in unique
        if _clean_key(item.genre_key) != "unknown"
    }
    genre_cap = max(1, ceil(min(30, requested_limit) * 0.4))

    remaining = list(unique)
    selected: list[PopularCandidate[T]] = []
    artist_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}

    while remaining and len(selected) < requested_limit:
        position = len(selected)
        in_head = position < head_size
        current_cap = head_artist_cap if in_head else overall_artist_cap
        recent_artists = {
            _clean_key(item.artist_key)
            for item in selected[-artist_gap:]
        }

        require_new_artist = False
        if in_head and target_head_artists:
            unique_selected = len(artist_counts)
            slots_after_pick = head_size - position - 1
            require_new_artist = unique_selected < target_head_artists and (
                slots_after_pick < target_head_artists - unique_selected
            )

        eligible = _eligible_candidates(
            remaining,
            artist_counts=artist_counts,
            genre_counts=genre_counts,
            artist_cap=current_cap,
            recent_artists=recent_artists,
            require_new_artist=require_new_artist,
            enforce_genre=len(useful_genres) > 1 and position < 30,
            genre_cap=genre_cap,
        )
        if not eligible:
            eligible = _eligible_candidates(
                remaining,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                artist_cap=current_cap,
                recent_artists=recent_artists,
                require_new_artist=require_new_artist,
                enforce_genre=False,
                genre_cap=genre_cap,
            )
        if not eligible:
            eligible = _eligible_candidates(
                remaining,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                artist_cap=current_cap,
                recent_artists=set(),
                require_new_artist=False,
                enforce_genre=False,
                genre_cap=genre_cap,
            )
        if not eligible:
            # A very small or single-artist catalog must still be usable. Caps
            # are relaxed only after every diverse option has been exhausted.
            eligible = list(remaining)

        chosen = max(
            eligible,
            key=lambda item: _selection_score(
                item,
                rotation_key=rotation_key,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                region_counts=region_counts,
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)

        artist_key = _clean_key(chosen.artist_key)
        genre_key = _clean_key(chosen.genre_key)
        region_key = _clean_key(chosen.region_key)
        artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if genre_key != "unknown":
            genre_counts[genre_key] = genre_counts.get(genre_key, 0) + 1
        if region_key != "unknown":
            region_counts[region_key] = region_counts.get(region_key, 0) + 1

    return selected


def _eligible_candidates(
    candidates: Iterable[PopularCandidate[T]],
    *,
    artist_counts: dict[str, int],
    genre_counts: dict[str, int],
    artist_cap: int,
    recent_artists: set[str],
    require_new_artist: bool,
    enforce_genre: bool,
    genre_cap: int,
) -> list[PopularCandidate[T]]:
    result: list[PopularCandidate[T]] = []
    for candidate in candidates:
        artist_key = _clean_key(candidate.artist_key)
        genre_key = _clean_key(candidate.genre_key)
        if artist_counts.get(artist_key, 0) >= artist_cap:
            continue
        if artist_key in recent_artists:
            continue
        if require_new_artist and artist_key in artist_counts:
            continue
        if enforce_genre and genre_key != "unknown" and genre_counts.get(genre_key, 0) >= genre_cap:
            continue
        result.append(candidate)
    return result


def _selection_score(
    candidate: PopularCandidate[T],
    *,
    rotation_key: str,
    artist_counts: dict[str, int],
    genre_counts: dict[str, int],
    region_counts: dict[str, int],
) -> float:
    artist_key = _clean_key(candidate.artist_key)
    genre_key = _clean_key(candidate.genre_key)
    region_key = _clean_key(candidate.region_key)
    has_evidence = any(
        (
            candidate.provider_signal_reliable,
            candidate.artist_canonical,
            candidate.artist_followers,
            candidate.unique_listeners,
            candidate.capped_plays,
            candidate.favorite_count,
            candidate.playlist_add_count,
        )
    )
    relevance = (
        popular_candidate_score(candidate)
        if has_evidence
        else float(candidate.popularity_score) * 0.72 + float(candidate.quality_score) * 0.28
    )
    rotation = _stable_fraction(rotation_key, candidate.stable_key) * 4.0
    artist_penalty = artist_counts.get(artist_key, 0) * 16.0
    genre_penalty = genre_counts.get(genre_key, 0) * 1.2 if genre_key != "unknown" else 0.0
    region_penalty = region_counts.get(region_key, 0) * 0.15 if region_key != "unknown" else 0.0
    return relevance + rotation - artist_penalty - genre_penalty - region_penalty


def _stable_fraction(rotation_key: str, stable_key: str) -> float:
    digest = blake2b(f"{rotation_key}:{stable_key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float((1 << 64) - 1)


def _clean_key(value: str | None) -> str:
    return str(value or "").strip().lower() or "unknown"


def _clamp_score(value: object) -> float:
    try:
        return max(0.0, min(100.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _optional_non_negative_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _log_score(value: float | int, reference: float | int) -> float:
    safe_value = max(0.0, float(value or 0.0))
    safe_reference = max(1.0, float(reference or 1.0))
    return _clamp_score(log1p(safe_value) / log1p(safe_reference) * 100.0)
