import os


def get_float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


PROVIDER_REQUEST_DELAY_SECONDS = get_float_env("PROVIDER_REQUEST_DELAY_SECONDS", 0.4)
PROVIDER_TIMEOUT_SECONDS = get_float_env("PROVIDER_TIMEOUT_SECONDS", 15.0)
IMPORT_BATCH_LIMIT = get_int_env("IMPORT_BATCH_LIMIT", 5)
IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT = get_int_env("IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT", 100)
