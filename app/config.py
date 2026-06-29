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


PROVIDER_REQUEST_DELAY_SECONDS = get_float_env("PROVIDER_REQUEST_DELAY_SECONDS", 0.4)
PROVIDER_TIMEOUT_SECONDS = get_float_env("PROVIDER_TIMEOUT_SECONDS", 15.0)
IMPORT_BATCH_LIMIT = get_int_env("IMPORT_BATCH_LIMIT", 5)
IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT = get_int_env("IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT", 100)

APP_AUTH_TOKEN = os.getenv(
    "MUSIC_APP_AUTH_TOKEN",
    "sha256:0e7d2d2c6b6d4d83a834bbf9f6f1a012b6d1c38f0d5c9f9a67db2c7c2ad1e9c1",
)
ADMIN_API_KEY = os.getenv(
    "MUSIC_ADMIN_API_KEY",
    "admin_6b5e5f2d8c8d45d2b74573d0e2b681b0",
)


def token_matches(expected: str, received: str | None) -> bool:
    return bool(received) and secrets.compare_digest(expected, received)
