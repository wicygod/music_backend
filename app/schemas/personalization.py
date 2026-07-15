from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class OnboardingArtistRead(CamelModel):
    id: int
    name: str
    avatar_url: str | None = Field(default=None, alias="avatarUrl")
    genres: list[str] = Field(default_factory=list)
    popularity_score: float = Field(default=0.0, alias="popularityScore")
    track_count: int = Field(default=0, alias="trackCount")
    selected: bool = False


class OnboardingArtistsResponse(CamelModel):
    items: list[OnboardingArtistRead] = Field(default_factory=list)
    total: int
    page: int
    limit: int
    has_more: bool = Field(alias="hasMore")
    minimum_required: int = Field(default=3, alias="minimumRequired")


class MusicPreferencesUpdate(CamelModel):
    artist_ids: list[int] = Field(default_factory=list, alias="artistIds", max_length=200)
    source: Literal["onboarding", "settings"] = "onboarding"
    skipped: bool = False

    @field_validator("artist_ids")
    @classmethod
    def deduplicate_artist_ids(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))


class UserArtistPreferenceRead(CamelModel):
    artist_id: int = Field(alias="artistId")
    source: str
    explicit_selected: bool = Field(alias="explicitSelected")
    is_hidden: bool = Field(alias="isHidden")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class MusicPreferencesRead(CamelModel):
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    selected_artist_ids: list[int] = Field(default_factory=list, alias="selectedArtistIds")
    items: list[UserArtistPreferenceRead] = Field(default_factory=list)


class ListeningEventCreate(CamelModel):
    event_id: str = Field(alias="eventId", min_length=8, max_length=128)
    track_id: int = Field(alias="trackId", gt=0)
    artist_id: int | None = Field(default=None, alias="artistId", gt=0)
    started_at: datetime = Field(alias="startedAt")
    listened_duration_seconds: int = Field(alias="listenedDuration", ge=0, le=86_400)
    track_duration_seconds: int | None = Field(default=None, alias="trackDuration", ge=0, le=86_400)
    completion_ratio: float | None = Field(default=None, alias="completionRatio", ge=0, le=1)
    completed: bool = False
    skipped: bool = False
    context: str = Field(default="unknown", min_length=1, max_length=64)
    recommendation_type: str | None = Field(default=None, alias="recommendationType", max_length=64)
    recommendation_reason: str | None = Field(default=None, alias="recommendationReason", max_length=255)
    algorithm_version: str | None = Field(default=None, alias="algorithmVersion", max_length=64)


class ListeningEventRead(ListeningEventCreate):
    id: int
    created_at: datetime = Field(alias="createdAt")


RecommendationEventType = Literal[
    "recommendation_impression",
    "recommendation_played",
    "recommendation_skipped",
    "recommendation_liked",
]


class RecommendationEventCreate(CamelModel):
    event_id: str = Field(alias="eventId", min_length=8, max_length=128)
    track_id: int = Field(alias="trackId", gt=0)
    event_type: RecommendationEventType = Field(alias="eventType")
    position: int | None = Field(default=None, ge=0)
    recommendation_type: str = Field(default="unknown", alias="recommendationType", min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=255)
    algorithm_version: str = Field(default="v1", alias="algorithmVersion", min_length=1, max_length=64)
    context: str = Field(default="home", min_length=1, max_length=64)


class RecommendationEventRead(RecommendationEventCreate):
    id: int
    created_at: datetime = Field(alias="createdAt")


MusicSignalType = Literal[
    "like",
    "unlike",
    "playlist",
    "playlist_remove",
    "follow",
    "artist_view",
    "hide",
    "unhide",
]


class MusicSignalCreate(CamelModel):
    event_id: str = Field(alias="eventId", min_length=8, max_length=128)
    signal: MusicSignalType
    track_id: int = Field(alias="trackId", gt=0)
    artist_id: int | None = Field(default=None, alias="artistId", gt=0)
    context: str = Field(default="unknown", min_length=1, max_length=64)
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")


class MusicSignalRead(CamelModel):
    event_id: str = Field(alias="eventId")
    signal: MusicSignalType
    created: bool
