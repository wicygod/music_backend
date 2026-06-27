from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.track import TrackRead


class PlaylistCreate(BaseModel):
    name: str
    description: str | None = None
    user_id: str = "local-user"


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
