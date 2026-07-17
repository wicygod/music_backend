from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ArtistBase(BaseModel):
    name: str
    avatar_url: str | None = None
    region: str = "unknown"
    genres: list[str] = Field(default_factory=list)
    popularity_score: float = 0.0
    source_name: str | None = None
    source_external_id: str | None = None
    source_url: str | None = None
    confidence_score: float = 1.0
    needs_review: bool = False
    priority: str = "normal"
    tracks_target: int = 25
    seed_source: str | None = None
    import_status: str = "pending"
    last_imported_at: datetime | None = None


class ArtistSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    avatar_url: str | None = None
    region: str = "unknown"
    is_canonical: bool = False
    source_verified: bool = False
    source_followers_count: int = 0
    needs_review: bool = False


class ArtistRead(ArtistBase):
    id: int
    normalized_name: str
    created_at: datetime
    updated_at: datetime


class ArtistWithTracks(ArtistRead):
    track_count: int = 0


class ArtistListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ArtistRead]
