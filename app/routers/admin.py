from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, selectinload

from app.config import ADMIN_API_KEY, token_matches
from app.database import get_db
from app.models.history import ListeningHistory
from app.models.track import Track, TrackArtist
from app.models.user import BlockedUser, User
from app.schemas.auth import BanRequest
from app.services.admin_monitor import recent_events, system_stats
from app.services.auth_service import user_to_read
from app.services.serialization_service import track_to_read


router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin_key(x_admin_key: str | None = Header(None, alias="X-Admin-Key")) -> None:
    if not token_matches(ADMIN_API_KEY, x_admin_key):
        raise HTTPException(status_code=403, detail="Forbidden")


def _top_tracks(db: Session, limit: int = 10) -> list[dict]:
    stmt = (
        select(Track, func.count(ListeningHistory.id).label("play_count"))
        .join(ListeningHistory, ListeningHistory.track_id == Track.id)
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .group_by(Track.id)
        .order_by(desc("play_count"), Track.title.asc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "track": track_to_read(track).model_dump(mode="json"),
            "play_count": int(play_count),
        }
        for track, play_count in rows
    ]


@router.get("/stats", dependencies=[Depends(require_admin_key)])
def admin_stats(db: Session = Depends(get_db)) -> dict:
    stats = system_stats()
    stats["top_tracks"] = _top_tracks(db, limit=10)
    return stats


@router.get("/logs", dependencies=[Depends(require_admin_key)])
def admin_logs(limit: int = Query(80, ge=1, le=300)) -> dict:
    return {"events": recent_events(limit=limit)}


@router.get("/users", dependencies=[Depends(require_admin_key)])
def admin_users(db: Session = Depends(get_db)) -> dict:
    users = (
        db.execute(
            select(User)
            .options(selectinload(User.block))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(200)
        )
        .scalars()
        .all()
    )
    return {"users": [user_to_read(user).model_dump(mode="json") for user in users]}


@router.post("/users/{user_id}/ban", dependencies=[Depends(require_admin_key)])
def ban_user(user_id: int, payload: BanRequest | None = None, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.block:
        db.add(BlockedUser(user_id=user.id, reason=(payload.reason if payload else None) or "manual admin ban"))
        db.commit()
        db.refresh(user)
    return {"ok": True, "user": user_to_read(user).model_dump(mode="json")}
