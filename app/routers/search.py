from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.track import TrackRead
from app.services.search_service import search_local_catalog


router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=list[TrackRead])
def search(
    background_tasks: BackgroundTasks,
    q: str = Query(..., min_length=1),
    limit: int = Query(150, ge=1, le=150),
    db: Session = Depends(get_db),
) -> list[TrackRead]:
    return search_local_catalog(db, q, background_tasks, limit=limit)
