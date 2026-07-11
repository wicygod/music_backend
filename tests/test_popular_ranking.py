from collections import Counter

from app.services.popular_ranking_service import PopularCandidate, rank_popular_candidates


def candidate(
    track_id: str,
    artist: str,
    *,
    popularity: float = 75,
    quality: float = 100,
    genre: str = "alt",
    region: str = "global",
) -> PopularCandidate[str]:
    return PopularCandidate(
        item=track_id,
        stable_key=track_id,
        artist_key=artist,
        genre_key=genre,
        region_key=region,
        popularity_score=popularity,
        quality_score=quality,
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
