from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.tracks import get_track
from app.schemas.track import TrackRead
from app.services.serialization_service import track_to_read


router = APIRouter(prefix="/api/tracks", tags=["tracks"])


@router.get("/{track_id}", response_model=TrackRead)
def read_track(track_id: int, db: Session = Depends(get_db)) -> TrackRead:
    track = get_track(db, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track_to_read(track)
