from datetime import datetime

from pydantic import BaseModel, Field


class UserRead(BaseModel):
    id: int
    login: str
    nickname: str
    avatar_url: str | None = None
    subscription_status: str = "inactive"
    is_premium: bool = False
    music_preferences_completed_at: datetime | None = None
    created_at: datetime
    is_banned: bool = False


class AuthRequest(BaseModel):
    login: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class RegisterRequest(AuthRequest):
    nickname: str = Field(min_length=2, max_length=96)


class AuthResponse(BaseModel):
    token: str
    user: UserRead


class NicknameUpdate(BaseModel):
    nickname: str = Field(min_length=2, max_length=96)


class AvatarUpdate(BaseModel):
    avatar_data_url: str = Field(min_length=16, max_length=2_000_000)


class BanRequest(BaseModel):
    reason: str | None = Field(default="manual admin ban", max_length=255)
