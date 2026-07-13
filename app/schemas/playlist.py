from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.track import TrackRead


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Playlist name cannot be blank")
        return cleaned

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PlaylistRead(BaseModel):
    id: int
    user_id: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    tracks: list[TrackRead] = Field(default_factory=list)


class FavoriteRead(BaseModel):
    user_id: str
    track: TrackRead
    created_at: datetime
