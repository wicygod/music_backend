import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.models.user import User
from app.routers.auth import register
from app.schemas.auth import RegisterRequest
from app.services.auth_service import decode_access_token, verify_password


def test_registration_persists_normalized_account_and_returns_token() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        response = register(
            RegisterRequest(login=" New.Listener ", nickname="New Listener", password="secret42"),
            db,
        )
        user = db.execute(select(User).where(User.login == "new.listener")).scalar_one()

    engine.dispose()
    assert response.user.id == user.id
    assert response.user.login == "new.listener"
    assert decode_access_token(response.token)["sub"] == str(user.id)
    assert verify_password("secret42", user.password_hash)
