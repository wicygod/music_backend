from types import SimpleNamespace

from app.services.search_service import _is_allowed_provider_entry, _prefer_title_matches, _provider_query_relevance


def test_exact_artist_matches_rank_before_title_only_matches() -> None:
    artist_match = SimpleNamespace(
        title="Цветы",
        artist_links=[SimpleNamespace(artist=SimpleNamespace(name="Тёмный Принц"))],
    )
    title_match = SimpleNamespace(title="Темный принц и друзья", artist_links=[])

    ranked = _prefer_title_matches([title_match, artist_match], "темный принц")

    assert ranked == [artist_match, title_match]


def test_provider_relevance_prioritizes_artist_and_rejects_unrelated_items() -> None:
    exact_artist = {"title": "Цветы", "uploader": "Тёмный Принц"}
    title_match = {"title": "Тёмный принц и друзья", "uploader": "Tewiq"}
    unrelated = {"title": "Случайная песня", "uploader": "Другой артист"}

    assert _provider_query_relevance("темный принц", exact_artist) == 4
    assert _provider_query_relevance("темный принц", title_match) == 2
    assert _provider_query_relevance("темный принц", unrelated) == 0


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
