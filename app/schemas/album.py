from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.artist import ArtistSummary
from app.schemas.track import TrackRead


class AlbumSummary(BaseModel):
    id: int
    title: str
    album_type: str = "album"
    cover_url: str | None = None
    release_date: datetime | None = None
    track_count: int = 0
    artist: ArtistSummary


class AlbumRead(AlbumSummary):
    source_name: str
    source_url: str
    popularity_score: float = 0.0
    is_available: bool = True
    matched_track_id: int | None = None
    tracks: list[TrackRead] = Field(default_factory=list)


class SearchOverview(BaseModel):
    tracks: list[TrackRead] = Field(default_factory=list)
    albums: list[AlbumRead] = Field(default_factory=list)
    refresh_pending: bool = False
