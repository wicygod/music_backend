from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.feed import HomeFeed
from app.schemas.personalization import RecommendationEventCreate, RecommendationEventRead
from app.services.admin_monitor import record_event
from app.services.feed_service import get_home_feed
from app.services.preference_service import PreferenceServiceError, record_music_signal


router = APIRouter(prefix="/api/feed", tags=["feed"])


@router.get("/home", response_model=HomeFeed)
def home_feed(
    request: Request,
    db: Session = Depends(get_db),
) -> HomeFeed:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return get_home_feed(db, user_id=f"account:{user_id}")


@router.post("/events", response_model=RecommendationEventRead)
def create_recommendation_event(
    payload: RecommendationEventCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> RecommendationEventRead:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    try:
        result = record_music_signal(
            db,
            user_id=int(user_id),
            event_id=payload.event_id,
            track_id=payload.track_id,
            event_type=payload.event_type,
            position=payload.position,
            recommendation_type=payload.recommendation_type,
            reason=payload.reason,
            algorithm_version=payload.algorithm_version,
            context=payload.context,
            signal_type="analytics_only",
        )
    except PreferenceServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    event = result.event
    if result.created:
        record_event(event.event_type, f"Recommendation event for track {event.track_id}", path="/api/feed/events")
    return RecommendationEventRead(
        id=event.id,
        event_id=event.event_id,
        track_id=event.track_id,
        event_type=event.event_type,
        position=event.position,
        recommendation_type=event.recommendation_type,
        reason=event.reason,
        algorithm_version=event.algorithm_version,
        context=event.context,
        created_at=event.created_at,
    )
