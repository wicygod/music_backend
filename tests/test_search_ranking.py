from types import SimpleNamespace

from app.services.search_service import _prefer_title_matches, _provider_query_relevance


def test_title_matches_are_ranked_first_without_dropping_artist_matches() -> None:
    artist_match = SimpleNamespace(title="Цветы")
    title_match = SimpleNamespace(title="Темный принц и друзья")

    ranked = _prefer_title_matches([artist_match, title_match], "темный принц")

    assert ranked == [title_match, artist_match]


def test_provider_relevance_prioritizes_artist_and_rejects_unrelated_items() -> None:
    exact_artist = {"title": "Цветы", "uploader": "Тёмный Принц"}
    title_match = {"title": "Тёмный принц и друзья", "uploader": "Tewiq"}
    unrelated = {"title": "Случайная песня", "uploader": "Другой артист"}

    assert _provider_query_relevance("темный принц", exact_artist) == 4
    assert _provider_query_relevance("темный принц", title_match) == 2
    assert _provider_query_relevance("темный принц", unrelated) == 0
