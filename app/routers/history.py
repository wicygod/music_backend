from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.history import list_recent_history_tracks, record_track_play
from app.schemas.track import TrackRead
from app.services.device_service import get_device_id
from app.services.serialization_service import track_to_read


router = APIRouter(prefix="/api/history", tags=["history"])


@router.post("/listen/{track_id}", response_model=TrackRead)
def listen_track(
    track_id: int,
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
) -> TrackRead:
    track = record_track_play(db, track_id=track_id, user_id=device_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track_to_read(track)


@router.get("/recent", response_model=list[TrackRead])
def recent_history(
    limit: int = Query(36, ge=1, le=100),
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
) -> list[TrackRead]:
    return [track_to_read(track) for track in list_recent_history_tracks(db, limit=limit, user_id=device_id)]
