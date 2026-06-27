from pydantic import BaseModel, ConfigDict, Field

from app.schemas.track import TrackRead


class HomeFeed(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recent: list[TrackRead]
    random: list[TrackRead]
    trending: list[TrackRead]
    ru: list[TrackRead]
    global_: list[TrackRead] = Field(serialization_alias="global")
