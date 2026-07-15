from pydantic import BaseModel, ConfigDict, Field

from app.schemas.track import TrackRead


class RecommendationTrack(BaseModel):
    track: TrackRead
    recommendation_type: str
    reason: str
    algorithm_version: str


class HomeFeed(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recent: list[TrackRead]
    random: list[TrackRead]
    trending: list[TrackRead]
    top: list[TrackRead] = Field(default_factory=list)
    ru: list[TrackRead]
    global_: list[TrackRead] = Field(serialization_alias="global")
    personalized: list[RecommendationTrack] = Field(default_factory=list)
    personalization_active: bool = False
    algorithm_version: str | None = None
    popular_algorithm_version: str | None = None
    popular_window_days: int = 14
