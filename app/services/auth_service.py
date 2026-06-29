import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import JWT_EXPIRES_SECONDS, JWT_SECRET
from app.models.user import BlockedUser, User
from app.schemas.auth import UserRead


LOGIN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")
HASH_ITERATIONS = 120_000


def normalize_login(login: str) -> str:
    value = (login or "").strip().lower()
    if not LOGIN_RE.match(value):
        raise HTTPException(status_code=422, detail="Login must contain latin letters, numbers, dot, dash or underscore")
    return value


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"pbkdf2_sha256${HASH_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = password_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def create_access_token(user_id: int) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + JWT_EXPIRES_SECONDS}
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        actual = _b64url_decode(signature_b64)
        if not hmac.compare_digest(actual, expected):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(payload_b64))
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid auth token") from exc


def user_to_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        login=user.login,
        nickname=user.nickname,
        avatar_url=user.avatar_url,
        subscription_status=user.subscription_status,
        created_at=user.created_at,
        is_banned=bool(user.block),
    )


def get_user_with_block(db: Session, user_id: int) -> User | None:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.block))
    return db.execute(stmt).scalars().first()


def require_active_user(db: Session, user_id: int) -> User:
    user = get_user_with_block(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.block:
        raise HTTPException(status_code=403, detail="Account is banned")
    return user


def is_user_banned(db: Session, user_id: int) -> bool:
    return bool(db.execute(select(BlockedUser.id).where(BlockedUser.user_id == user_id)).scalars().first())
