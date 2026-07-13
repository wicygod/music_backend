import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.routers.import_jobs import require_admin_key


def test_import_endpoints_require_admin_key() -> None:
    require_admin_key("test-admin-key")
    with pytest.raises(HTTPException) as exc_info:
        require_admin_key("wrong-key")
    assert exc_info.value.status_code == 403
