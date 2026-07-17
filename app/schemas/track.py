from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.artist import ArtistSummary


class TrackBase(BaseModel):
    title: str
    duration_seconds: int = 0
    cover_url: str | None = None
    genre: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    region: str = "unknown"
    popularity_score: float = 0.0
    quality_score: float = 0.0
    is_playable: bool = False
    audio_src: str | None = None
    source_name: str | None = None
    source_external_id: str | None = None
    source_url: str | None = None
    needs_review: bool = False


class TrackRead(TrackBase):
    id: int
    normalized_title: str
    artists: list[ArtistSummary] = Field(default_factory=list)
    album_id: int | None = None
    album_name: str | None = None
    album_track_number: int | None = None
    created_at: datetime
    updated_at: datetime


class TrackSeedCreate(TrackBase):
    artist: str
    artist_region: str = "unknown"
    artist_avatar_url: str | None = None
    artist_genres: list[str] = Field(default_factory=list)
