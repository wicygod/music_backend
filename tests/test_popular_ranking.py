import os
from collections import Counter

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.services.popular_ranking_service import (
    PopularCandidate,
    is_popular_candidate_eligible,
    local_engagement_score,
    popular_candidate_score,
    provider_popularity_score,
    rank_popular_candidates,
)


def candidate(
    track_id: str,
    artist: str,
    *,
    popularity: float = 75,
    quality: float = 100,
    genre: str = "alt",
    region: str = "global",
    **signals,
) -> PopularCandidate[str]:
    return PopularCandidate(
        item=track_id,
        stable_key=track_id,
        artist_key=artist,
        genre_key=genre,
        region_key=region,
        popularity_score=popularity,
        quality_score=quality,
        **signals,
    )


def varied_catalog(artists: int = 16, tracks_per_artist: int = 4) -> list[PopularCandidate[str]]:
    return [
        candidate(
            f"{artist_index}-{track_index}",
            f"artist-{artist_index}",
            genre=f"genre-{artist_index % 4}",
            region="ru" if artist_index % 5 == 0 else "global",
        )
        for artist_index in range(artists)
        for track_index in range(tracks_per_artist)
    ]


def test_same_rotation_key_is_stable_even_if_input_order_changes() -> None:
    items = varied_catalog()

    first = rank_popular_candidates(items, limit=48, rotation_key="2026-07-11:user")
    second = rank_popular_candidates(reversed(items), limit=48, rotation_key="2026-07-11:user")

    assert [item.stable_key for item in first] == [item.stable_key for item in second]


def test_new_rotation_key_rotates_equal_score_candidates() -> None:
    items = varied_catalog()

    first = rank_popular_candidates(items, limit=48, rotation_key="2026-07-11:user")
    second = rank_popular_candidates(items, limit=48, rotation_key="2026-07-12:user")

    first_keys = [item.stable_key for item in first]
    second_keys = [item.stable_key for item in second]
    assert first_keys != second_keys
    assert len(set(first_keys)) == len(first_keys) == 48
    assert len(set(second_keys)) == len(second_keys) == 48


def test_first_24_are_artist_diverse_and_have_no_close_repeats() -> None:
    ranked = rank_popular_candidates(varied_catalog(), limit=48, rotation_key="daily:user")
    artists = [item.artist_key for item in ranked[:24]]
    counts = Counter(artists)

    assert len(counts) >= 12
    assert max(counts.values()) <= 2
    assert all(artist not in artists[max(0, index - 3) : index] for index, artist in enumerate(artists))


def test_dominant_artist_cannot_take_over_the_head() -> None:
    items = [
        candidate(f"dominant-{index}", "dominant", popularity=100)
        for index in range(100)
    ]
    items.extend(
        candidate(f"other-{artist}-{track}", f"artist-{artist}", popularity=65)
        for artist in range(20)
        for track in range(2)
    )

    ranked = rank_popular_candidates(items, limit=24, rotation_key="daily:user")
    counts = Counter(item.artist_key for item in ranked)

    assert counts["dominant"] <= 2
    assert len(counts) >= 12


def test_single_artist_catalog_relaxes_caps_and_still_fills() -> None:
    items = [candidate(str(index), "only-artist") for index in range(10)]

    ranked = rank_popular_candidates(items, limit=10, rotation_key="daily:user")

    assert len(ranked) == 10
    assert len({item.stable_key for item in ranked}) == 10


def test_duplicate_keys_and_empty_limits_are_handled() -> None:
    items = [candidate("same", "a"), candidate("same", "b"), candidate("other", "b")]

    ranked = rank_popular_candidates(items, limit=10, rotation_key="daily:user")

    assert [item.stable_key for item in ranked].count("same") == 1
    assert len(ranked) == 2
    assert rank_popular_candidates(items, limit=0, rotation_key="daily:user") == []
    assert rank_popular_candidates([], limit=10, rotation_key="daily:user") == []


def test_relevance_difference_is_larger_than_rotation_jitter() -> None:
    strong = candidate("strong", "a", popularity=100, quality=100)
    weak = candidate("weak", "b", popularity=0, quality=0)

    for seed in ("day-1", "day-2", "day-3"):
        ranked = rank_popular_candidates([weak, strong], limit=2, rotation_key=seed)
        assert ranked[0].stable_key == "strong"


def test_provider_metrics_replace_the_old_constant_score() -> None:
    fallback, fallback_reliable = provider_popularity_score(fallback=75)
    small, small_reliable = provider_popularity_score(
        view_count=12_000,
        like_count=300,
        repost_count=5,
        timestamp=1_700_000_000,
        now_timestamp=1_710_000_000,
        fallback=75,
    )
    hit, hit_reliable = provider_popularity_score(
        view_count=3_600_000,
        like_count=56_000,
        repost_count=235,
        timestamp=1_700_000_000,
        now_timestamp=1_710_000_000,
        fallback=75,
    )

    assert fallback == 75
    assert fallback_reliable is False
    assert small_reliable is hit_reliable is True
    assert 0 < small < hit <= 100


def test_two_plays_do_not_qualify_a_low_authority_artist() -> None:
    item = candidate(
        "noise",
        "tiny-profile",
        artist_canonical=True,
        artist_followers=20,
        unique_listeners=1,
        capped_plays=2,
    )

    assert is_popular_candidate_eligible(item) is False


def test_canonical_large_artist_has_safe_cold_start() -> None:
    item = candidate(
        "new-release",
        "real-artist",
        artist_canonical=True,
        artist_followers=80_000,
        unique_listeners=0,
        capped_plays=0,
    )

    assert is_popular_candidate_eligible(item) is True


def test_reliable_low_provider_score_blocks_large_artist_cold_start() -> None:
    item = candidate(
        "provider-flop",
        "real-artist",
        popularity=12,
        provider_signal_reliable=True,
        artist_canonical=True,
        artist_followers=2_000_000,
    )

    assert is_popular_candidate_eligible(item) is False


def test_unique_listeners_outweigh_one_account_on_repeat() -> None:
    repeated = candidate(
        "repeat",
        "artist-a",
        artist_canonical=True,
        artist_followers=50_000,
        unique_listeners=1,
        capped_plays=5,
    )
    community = candidate(
        "community",
        "artist-b",
        artist_canonical=True,
        artist_followers=50_000,
        unique_listeners=5,
        capped_plays=5,
    )

    assert local_engagement_score(community) > local_engagement_score(repeated)
    assert popular_candidate_score(community) > popular_candidate_score(repeated)


def test_completed_plays_help_and_quick_skips_hurt() -> None:
    completed = candidate(
        "completed",
        "artist-a",
        artist_canonical=True,
        artist_followers=50_000,
        unique_listeners=4,
        capped_plays=6,
        detailed_plays=5,
        completed_plays=5,
    )
    skipped = candidate(
        "skipped",
        "artist-b",
        artist_canonical=True,
        artist_followers=50_000,
        unique_listeners=4,
        capped_plays=6,
        detailed_plays=5,
        skipped_plays=5,
    )

    assert popular_candidate_score(completed) > popular_candidate_score(skipped)


def test_low_value_variant_needs_unusually_strong_evidence() -> None:
    weak_variant = candidate(
        "slowed",
        "artist-a",
        artist_canonical=True,
        artist_followers=100_000,
        low_value_variant=True,
    )
    proven_variant = candidate(
        "popular-remix",
        "artist-a",
        popularity=90,
        provider_signal_reliable=True,
        artist_canonical=True,
        artist_followers=100_000,
        low_value_variant=True,
    )

    assert is_popular_candidate_eligible(weak_variant) is False
    assert is_popular_candidate_eligible(proven_variant) is True
