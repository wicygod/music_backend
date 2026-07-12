from app.services.normalization_service import detect_artist_region, normalize_name, normalize_title


def test_detect_artist_region_handles_cyrillic_and_latin_names() -> None:
    assert detect_artist_region("Моргенштерн") == "ru"
    assert detect_artist_region("Lil Peep") == "global"
    assert detect_artist_region("9mice / Kai Angel") == "global"
    assert detect_artist_region("★") == "unknown"


def test_normalization_treats_cyrillic_yo_as_e() -> None:
    assert normalize_name("Тёмный Принц") == normalize_name("Темный Принц")
    assert normalize_title("Всё моё") == normalize_title("Все мое")
