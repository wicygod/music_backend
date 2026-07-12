import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.services.auth_service import create_stream_ticket, decode_access_token, decode_stream_ticket


def test_stream_ticket_is_short_scoped_and_track_bound() -> None:
    ticket = create_stream_ticket(user_id=7, track_id=42)

    payload = decode_stream_ticket(ticket, track_id=42)

    assert payload["sub"] == "7"
    assert payload["scope"] == "stream"
    with pytest.raises(HTTPException):
        decode_stream_ticket(ticket, track_id=43)
    with pytest.raises(HTTPException):
        decode_access_token(ticket)
