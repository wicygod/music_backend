from sqlalchemy.orm import Session

from app.repositories.history import list_recent_history_tracks
from app.repositories.tracks import (
    list_random_tracks,
    list_region_tracks,
    list_trending_tracks,
)
from app.schemas.feed import HomeFeed
from app.services.serialization_service import track_to_read


def get_home_feed(db: Session, user_id: str = "local") -> HomeFeed:
    return HomeFeed(
        recent=[track_to_read(track) for track in list_recent_history_tracks(db, limit=36, user_id=user_id)],
        random=[track_to_read(track) for track in list_random_tracks(db, limit=48)],
        trending=[track_to_read(track) for track in list_trending_tracks(db, limit=18)],
        ru=[track_to_read(track) for track in list_region_tracks(db, "ru", limit=18)],
        global_=[track_to_read(track) for track in list_region_tracks(db, "global", limit=18)],
    )
