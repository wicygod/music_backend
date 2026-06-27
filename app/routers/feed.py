from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.feed import HomeFeed
from app.services.feed_service import get_home_feed


router = APIRouter(prefix="/api/feed", tags=["feed"])


@router.get("/home", response_model=HomeFeed)
def home_feed(db: Session = Depends(get_db)) -> HomeFeed:
    return get_home_feed(db)
