import os
import secrets


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


def get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured in the service environment")
    return value


PROVIDER_REQUEST_DELAY_SECONDS = get_float_env("PROVIDER_REQUEST_DELAY_SECONDS", 0.4)
PROVIDER_TIMEOUT_SECONDS = get_float_env("PROVIDER_TIMEOUT_SECONDS", 15.0)
IMPORT_BATCH_LIMIT = get_int_env("IMPORT_BATCH_LIMIT", 5)
IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT = get_int_env("IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT", 100)

APP_AUTH_TOKEN = get_required_env("MUSIC_APP_AUTH_TOKEN")
ADMIN_API_KEY = get_required_env("MUSIC_ADMIN_API_KEY")
JWT_SECRET = get_required_env("MUSIC_JWT_SECRET")
JWT_EXPIRES_SECONDS = get_int_env("MUSIC_JWT_EXPIRES_SECONDS", 60 * 60 * 24 * 30)
BUGREPORT_SERVICE_URL = os.getenv("BUGREPORT_SERVICE_URL", "http://127.0.0.1:8001/api/bugreport")


def token_matches(expected: str, received: str | None) -> bool:
    return bool(received) and secrets.compare_digest(expected, received)
