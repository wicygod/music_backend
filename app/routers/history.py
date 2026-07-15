from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.history import (
    add_listening_time,
    get_history_summary,
    list_recent_history_tracks,
    record_listening_event,
    record_track_play,
)
from app.schemas.personalization import ListeningEventCreate, ListeningEventRead
from app.schemas.track import TrackRead
from app.services.admin_monitor import record_event
from app.services.serialization_service import track_to_read


router = APIRouter(prefix="/api/history", tags=["history"])


class ListeningProgress(BaseModel):
    seconds: int = Field(ge=1, le=300)


def _account_user_id(request: Request) -> int:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return int(user_id)


def _history_user_id(request: Request) -> str:
    return f"account:{_account_user_id(request)}"


@router.post("/events", response_model=ListeningEventRead)
def create_listening_event(
    payload: ListeningEventCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> ListeningEventRead:
    account_id = _account_user_id(request)
    try:
        item, created = record_listening_event(db, account_id=account_id, payload=payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        record_event(
            "listening",
            f"Playback event stored for track {item.track_id}",
            path="/api/history/events",
        )
        if item.recommendation_type:
            analytics_kind = "recommendation_skipped" if item.skipped else "recommendation_played"
            record_event(
                analytics_kind,
                f"Recommendation playback for track {item.track_id}",
                path="/api/history/events",
            )
    return ListeningEventRead(
        id=item.id,
        event_id=item.event_id or payload.event_id,
        track_id=item.track_id,
        artist_id=item.artist_id,
        started_at=item.started_at or item.played_at,
        listened_duration_seconds=item.listened_duration_seconds,
        track_duration_seconds=item.track_duration_seconds,
        completion_ratio=item.completion_ratio,
        completed=item.completed,
        skipped=item.skipped,
        context=item.context,
        recommendation_type=item.recommendation_type,
        recommendation_reason=item.recommendation_reason,
        algorithm_version=item.algorithm_version,
        created_at=item.created_at,
    )


@router.post("/listen/{track_id}", response_model=TrackRead)
def listen_track(
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> TrackRead:
    track = record_track_play(db, track_id=track_id, user_id=_history_user_id(request))
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track_to_read(track)


@router.get("/recent", response_model=list[TrackRead])
def recent_history(
    request: Request,
    limit: int = Query(36, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[TrackRead]:
    return [track_to_read(track) for track in list_recent_history_tracks(db, limit=limit, user_id=_history_user_id(request))]


@router.get("/summary")
def history_summary(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    account_id = _account_user_id(request)
    return get_history_summary(db, user_id=f"account:{account_id}", account_id=account_id)


@router.post("/progress")
def listening_progress(
    payload: ListeningProgress,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    account_id = _account_user_id(request)
    add_listening_time(db, account_id=account_id, seconds=payload.seconds)
    return get_history_summary(db, user_id=f"account:{account_id}", account_id=account_id)
