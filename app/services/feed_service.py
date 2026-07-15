from sqlalchemy.orm import Session

from app.repositories.history import list_recent_history_tracks
from app.repositories.tracks import (
    list_random_tracks,
    list_region_tracks,
    list_trending_rankings,
)
from app.schemas.feed import HomeFeed
from app.services.recommendation_config import RECOMMENDATION_CONFIG
from app.services.recommendation_service import get_personalized_recommendations
from app.services.popular_ranking_service import POPULAR_ALGORITHM_VERSION
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
    popular_rankings = list_trending_rankings(
        db,
        limit=48,
        rotation_key="global",
        excluded_song_keys=seen_keys,
    )
    popular = dedupe_tracks(
        [candidate.item for candidate in popular_rankings],
        limit=48,
        seen_keys=seen_keys,
    )
    random = dedupe_tracks(list_random_tracks(db, limit=160), limit=48, seen_keys=seen_keys)
    ru = dedupe_tracks(list_region_tracks(db, "ru", limit=72), limit=18, seen_keys=seen_keys)
    global_tracks = dedupe_tracks(list_region_tracks(db, "global", limit=72), limit=18, seen_keys=seen_keys)
    return HomeFeed(
        recent=[track_to_read(track) for track in recent],
        random=[track_to_read(track) for track in random],
        trending=[track_to_read(track) for track in popular],
        top=[track_to_read(track) for track in popular],
        ru=[track_to_read(track) for track in ru],
        global_=[track_to_read(track) for track in global_tracks],
        personalized=personalized,
        personalization_active=bool(recommendation_result and recommendation_result.personalization_active),
        algorithm_version=RECOMMENDATION_CONFIG.algorithm_version,
        popular_algorithm_version=POPULAR_ALGORITHM_VERSION,
        popular_window_days=14,
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
