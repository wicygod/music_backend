from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.feed import HomeFeed
from app.services.feed_service import get_home_feed


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
