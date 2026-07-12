from types import SimpleNamespace

from app.services.search_service import _prefer_title_matches


def test_title_matches_are_ranked_first_without_dropping_artist_matches() -> None:
    artist_match = SimpleNamespace(title="Цветы")
    title_match = SimpleNamespace(title="Темный принц и друзья")

    ranked = _prefer_title_matches([artist_match, title_match], "темный принц")

    assert ranked == [title_match, artist_match]
