import os
from types import SimpleNamespace

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services.normalization_service import normalize_name, normalize_title
from app.services import search_service
from app.services.search_service import (
    _is_allowed_provider_entry,
    _prefer_title_matches,
    _provider_query_relevance,
    _relevance_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _track(title: str, artist: str = "") -> SimpleNamespace:
    """Build a lightweight Track-like object with artist_links."""
    links = []
    if artist:
        links.append(SimpleNamespace(artist=SimpleNamespace(name=artist)))
    return SimpleNamespace(title=title, artist_links=links)


def _dict_entry(title: str, uploader: str = "") -> dict:
    """Build a lightweight provider dict entry."""
    entry: dict = {"title": title}
    if uploader:
        entry["uploader"] = uploader
    return entry


# ---------------------------------------------------------------------------
# 1. Exact title match has highest priority
# ---------------------------------------------------------------------------

def test_exact_title_match_ranks_above_artist_match() -> None:
    """Query 'Trinity': track named 'Trinity' > track by artist 'Trinity'."""
    exact_title = _track("Trinity", artist="SomeArtist")
    artist_match = _track("Night Drive", artist="Trinity")

    ranked = _prefer_title_matches([artist_match, exact_title], "Trinity")

    assert ranked[0] is exact_title


def test_exact_title_match_all_i_need() -> None:
    """Query 'All I Need': song 'All I Need' > song by artist 'All I Need'."""
    exact_title = _track("All I Need", artist="Clams Casino")
    artist_match = _track("Unrelated Song", artist="All I Need")

    ranked = _prefer_title_matches([artist_match, exact_title], "All I Need")

    assert ranked[0] is exact_title


# ---------------------------------------------------------------------------
# 2. Title with stripped "Artist - " prefix counts as exact title match
# ---------------------------------------------------------------------------

def test_artist_prefix_stripped_title_is_high_priority() -> None:
    """'Clams Casino - All I Need' stripped to 'All I Need' → tier 1."""
    score = _relevance_score("All I Need", "Clams Casino - All I Need", "Clams Casino")
    assert score == 1  # tier 1 – exact after prefix strip


def test_prefix_stripped_title_ranks_above_artist_match() -> None:
    """Stripped title match should rank above exact artist match."""
    stripped = _track("Clams Casino - All I Need", artist="Clams Casino")
    artist_match = _track("Random Track", artist="All I Need")

    ranked = _prefer_title_matches([artist_match, stripped], "All I Need")

    assert ranked[0] is stripped


# ---------------------------------------------------------------------------
# 3. Exact artist match ranks after exact title but before token matches
# ---------------------------------------------------------------------------

def test_exact_artist_matches_rank_before_title_only_matches() -> None:
    """Original test: exact artist 'Тёмный Принц' > partial title match."""
    artist_match = SimpleNamespace(
        title="Цветы",
        artist_links=[SimpleNamespace(artist=SimpleNamespace(name="Тёмный Принц"))],
    )
    title_match = SimpleNamespace(title="Темный принц и друзья", artist_links=[])

    ranked = _prefer_title_matches([title_match, artist_match], "темный принц")

    assert ranked == [artist_match, title_match]


def test_exact_artist_above_partial_title() -> None:
    """Query 'Тёмный Принц' without an exact title — artist wins."""
    artist_track = _track("Цветы", artist="Тёмный Принц")
    partial_title = _track("Тёмный принц и друзья", artist="Other")

    ranked = _prefer_title_matches([partial_title, artist_track], "Тёмный Принц")

    assert ranked[0] is artist_track


# ---------------------------------------------------------------------------
# 4. All query words in title
# ---------------------------------------------------------------------------

def test_all_words_in_title() -> None:
    score = _relevance_score("dark night", "A Dark Night", "Random Artist")
    assert score == 3  # tier 3


# ---------------------------------------------------------------------------
# 5. All query words in artist
# ---------------------------------------------------------------------------

def test_all_words_in_artist() -> None:
    score = _relevance_score("clams casino", "Random Title", "Clams Casino Extras")
    assert score == 4  # tier 4 – all tokens match inside artist, but not exact


# ---------------------------------------------------------------------------
# 6. Combined artist + title match
# ---------------------------------------------------------------------------

def test_clams_casino_all_i_need_combined() -> None:
    """Query 'Clams Casino All I Need' matches via artist + title."""
    correct = _track("All I Need", artist="Clams Casino")
    unrelated = _track("Something Else", artist="Another")

    ranked = _prefer_title_matches([unrelated, correct], "Clams Casino All I Need")

    assert ranked[0] is correct


def test_combined_match_score() -> None:
    score = _relevance_score("Clams Casino All I Need", "All I Need", "Clams Casino")
    assert score == 5  # tier 5 – matched across artist + title


# ---------------------------------------------------------------------------
# 7. Stable ordering among equal-relevance items
# ---------------------------------------------------------------------------

def test_stable_order_among_equal_relevance() -> None:
    a = _track("Something", artist="X")
    b = _track("Another", artist="Y")
    c = _track("Third", artist="Z")

    ranked = _prefer_title_matches([a, b, c], "completely unrelated query word")

    # All tier-6 (no match) → preserved original order
    assert ranked == [a, b, c]


def test_stable_order_same_tier() -> None:
    """Two tracks both with exact title match → original order kept."""
    first = _track("Trinity", artist="Artist A")
    second = _track("Trinity", artist="Artist B")

    ranked = _prefer_title_matches([first, second], "Trinity")

    assert ranked == [first, second]


# ---------------------------------------------------------------------------
# 8. Typo tolerance still works (e.g. 'claims casino' → 'Clams Casino')
# ---------------------------------------------------------------------------

def test_clams_casino_is_found_when_query_contains_claims_typo() -> None:
    entry = {
        "id": "claims-casino-all-i-need",
        "title": "Clams Casino - All I Need",
        "artist": "Clams Casino",
        "channel": "Clams Casino - Topic",
        "categories": ["Music"],
        "webpage_url": "https://music.youtube.com/watch?v=claims123",
        "duration": 204,
    }

    assert _provider_query_relevance("claims casino all i need", entry) > 0
    assert _is_allowed_provider_entry("youtube", entry)


def test_typo_tolerance_in_ranking() -> None:
    """'claims casino' → 'Clams Casino' should still match via fuzzy tokens."""
    track = _track("All I Need", artist="Clams Casino")
    unrelated = _track("Casino Royale", artist="Claims Department")

    ranked = _prefer_title_matches([unrelated, track], "claims casino all i need")

    assert ranked[0] is track


# ---------------------------------------------------------------------------
# 9. Works identically for Track objects and provider dicts
# ---------------------------------------------------------------------------

def test_prefer_title_matches_works_for_dicts() -> None:
    exact_title = _dict_entry("Trinity", uploader="SomeChannel")
    artist_match = _dict_entry("Night Drive", uploader="Trinity")

    ranked = _prefer_title_matches([artist_match, exact_title], "Trinity")

    assert ranked[0] is exact_title


# ---------------------------------------------------------------------------
# Provider relevance (used for external results)
# ---------------------------------------------------------------------------

def test_provider_relevance_prioritizes_artist_and_rejects_unrelated_items() -> None:
    exact_artist = {"title": "Цветы", "uploader": "Тёмный Принц"}
    title_match = {"title": "Тёмный принц и друзья", "uploader": "Tewiq"}
    unrelated = {"title": "Случайная песня", "uploader": "Другой артист"}

    score_artist = _provider_query_relevance("темный принц", exact_artist)
    score_title = _provider_query_relevance("темный принц", title_match)
    score_none = _provider_query_relevance("темный принц", unrelated)

    assert score_artist > score_title > score_none
    assert score_none == 0


def test_provider_relevance_exact_title_highest() -> None:
    exact_title = {"title": "Trinity", "uploader": "SomeChannel"}
    exact_artist = {"title": "Night Drive", "uploader": "Trinity"}

    assert _provider_query_relevance("Trinity", exact_title) > _provider_query_relevance("Trinity", exact_artist)


def test_exact_title_survives_a_popular_artist_candidate_flood(monkeypatch) -> None:
    """A low-popularity exact song title must be fetched before popular artist matches."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(search_service, "_schedule_hydration", lambda *_args, **_kwargs: None)

    with Session(engine) as db:
        exact_artist = Artist(
            name="Trinity",
            normalized_name=normalize_name("Trinity"),
            genres_json="[]",
            popularity_score=100,
            source_name="soundcloud",
            source_url="https://soundcloud.com/trinity",
        )
        other_artist = Artist(
            name="Small Artist",
            normalized_name=normalize_name("Small Artist"),
            genres_json="[]",
            popularity_score=1,
            source_name="soundcloud",
            source_url="https://soundcloud.com/small-artist",
        )
        db.add_all([exact_artist, other_artist])
        db.flush()

        exact_track = Track(
            title="Trinity",
            normalized_title=normalize_title("Trinity"),
            duration_seconds=180,
            tags_json="[]",
            region="global",
            popularity_score=1,
            quality_score=100,
            is_playable=True,
            source_name="soundcloud",
            source_external_id="exact-title",
            source_url="https://soundcloud.com/small-artist/trinity",
            needs_review=False,
        )
        db.add(exact_track)
        db.flush()
        db.add(TrackArtist(track_id=exact_track.id, artist_id=other_artist.id, role="main"))

        for index in range(30):
            popular_track = Track(
                title=f"Popular song {index}",
                normalized_title=normalize_title(f"Popular song {index}"),
                duration_seconds=180,
                tags_json="[]",
                region="global",
                popularity_score=1000 - index,
                quality_score=100,
                is_playable=True,
                source_name="soundcloud",
                source_external_id=f"artist-match-{index}",
                source_url=f"https://soundcloud.com/trinity/song-{index}",
                needs_review=False,
            )
            db.add(popular_track)
            db.flush()
            db.add(TrackArtist(track_id=popular_track.id, artist_id=exact_artist.id, role="main"))
        db.commit()

        results = search_service.search_local_catalog(db, "Trinity", limit=3)

    assert results
    assert results[0].title == "Trinity"
