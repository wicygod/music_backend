from sqlalchemy.orm import Session

from app.repositories.history import list_recent_history_tracks
from app.repositories.tracks import (
    list_random_tracks,
    list_region_tracks,
    list_trending_tracks,
)
from app.schemas.feed import HomeFeed
from app.services.recommendation_config import RECOMMENDATION_CONFIG
from app.services.recommendation_service import get_personalized_recommendations
from app.services.serialization_service import track_to_read
from app.services.track_filter_service import dedupe_tracks


def get_home_feed(db: Session, user_id: str = "local") -> HomeFeed:
    seen_keys: set[str] = set()
    recent = dedupe_tracks(list_recent_history_tracks(db, limit=60, user_id=user_id), limit=36, seen_keys=seen_keys)
    account_id = _account_id_from_scope(user_id)
    recommendation_result = (
        get_personalized_recommendations(db, user_id=account_id)
        if account_id is not None
        else None
    )
    recent_ids = {track.id for track in recent}
    personalized = [
        item
        for item in (recommendation_result.items if recommendation_result else [])
        if item.track.id not in recent_ids
    ]
    trending = dedupe_tracks(
        list_trending_tracks(
            db,
            limit=48,
            rotation_key=user_id,
            excluded_song_keys=seen_keys,
        ),
        limit=48,
        seen_keys=seen_keys,
    )
    random = dedupe_tracks(list_random_tracks(db, limit=160), limit=48, seen_keys=seen_keys)
    ru = dedupe_tracks(list_region_tracks(db, "ru", limit=72), limit=18, seen_keys=seen_keys)
    global_tracks = dedupe_tracks(list_region_tracks(db, "global", limit=72), limit=18, seen_keys=seen_keys)
    return HomeFeed(
        recent=[track_to_read(track) for track in recent],
        random=[track_to_read(track) for track in random],
        trending=[track_to_read(track) for track in trending],
        ru=[track_to_read(track) for track in ru],
        global_=[track_to_read(track) for track in global_tracks],
        personalized=personalized,
        personalization_active=bool(recommendation_result and recommendation_result.personalization_active),
        algorithm_version=RECOMMENDATION_CONFIG.algorithm_version,
    )


def _account_id_from_scope(user_id: str) -> int | None:
    value = str(user_id or "")
    if not value.startswith("account:"):
        return None
    try:
        parsed = int(value.split(":", 1)[1])
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
