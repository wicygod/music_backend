import re

from fastapi import Header, Query


DEFAULT_DEVICE_ID = "local"
DEVICE_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def normalize_device_id(value: str | None) -> str:
    cleaned = DEVICE_ID_RE.sub("-", (value or "").strip())[:128].strip("-")
    return cleaned or DEFAULT_DEVICE_ID


def get_device_id(
    x_device_id: str | None = Header(None, alias="X-Device-Id"),
    user_id: str | None = Query(None, min_length=1, max_length=128),
) -> str:
    return normalize_device_id(x_device_id or user_id)
