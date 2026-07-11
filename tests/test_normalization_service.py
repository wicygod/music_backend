from app.services.normalization_service import detect_artist_region


def test_detect_artist_region_handles_cyrillic_and_latin_names() -> None:
    assert detect_artist_region("Моргенштерн") == "ru"
    assert detect_artist_region("Lil Peep") == "global"
    assert detect_artist_region("9mice / Kai Angel") == "global"
    assert detect_artist_region("★") == "unknown"
