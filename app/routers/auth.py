from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.auth import AuthRequest, AuthResponse, AvatarUpdate, NicknameUpdate, RegisterRequest, UserRead
from app.services.auth_service import (
    create_access_token,
    hash_password,
    normalize_login,
    require_active_user,
    user_to_read,
    verify_password,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    return authorization.split(" ", 1)[1].strip()


def current_user(
    authorization: str | None = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> User:
    from app.services.auth_service import decode_access_token

    payload = decode_access_token(_bearer_token(authorization))
    return require_active_user(db, int(payload["sub"]))


@router.post("/register", response_model=AuthResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    login = normalize_login(payload.login)
    existing = db.execute(select(User).where(User.login == login)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Login already exists")
    user = User(
        login=login,
        nickname=payload.nickname.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    return AuthResponse(token=token, user=user_to_read(user))


@router.post("/login", response_model=AuthResponse)
def login(payload: AuthRequest, db: Session = Depends(get_db)) -> AuthResponse:
    login_value = normalize_login(payload.login)
    user = db.execute(select(User).where(User.login == login_value)).scalars().first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid login or password")
    user = require_active_user(db, user.id)
    return AuthResponse(token=create_access_token(user.id), user=user_to_read(user))


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(current_user)) -> UserRead:
    return user_to_read(user)


@router.patch("/me", response_model=UserRead)
def update_me(payload: NicknameUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> UserRead:
    user.nickname = payload.nickname.strip()
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_to_read(user)


@router.post("/me/avatar", response_model=UserRead)
def update_avatar(payload: AvatarUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> UserRead:
    if not payload.avatar_data_url.startswith("data:image/"):
        raise HTTPException(status_code=422, detail="Avatar must be an image data URL")
    user.avatar_url = payload.avatar_data_url
    db.add(user)
    db.commit()
    db.refresh(user)
    return user_to_read(user)
