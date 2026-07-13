from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.models.history import ListeningHistory
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.repositories.tracks import get_track


DEFAULT_USER_ID = "local"
def record_track_play(db: Session, track_id: int, user_id: str = DEFAULT_USER_ID) -> Track | None:
    track = get_track(db, track_id)
    if not track:
        return None

    stmt = select(ListeningHistory).where(
        ListeningHistory.user_id == user_id,
        ListeningHistory.track_id == track_id,
    )
    item = db.execute(stmt).scalars().first()
    if item:
        item.played_at = datetime.utcnow()
        item.play_count = max(1, int(item.play_count or 0)) + 1
    else:
        db.add(ListeningHistory(user_id=user_id, track_id=track_id, play_count=1, played_at=datetime.utcnow()))

    db.flush()
    db.commit()
    return get_track(db, track_id)


def list_recent_history_tracks(db: Session, limit: int = 36, user_id: str = DEFAULT_USER_ID) -> list[Track]:
    stmt = (
        select(Track)
        .join(ListeningHistory, ListeningHistory.track_id == Track.id)
        .where(ListeningHistory.user_id == user_id)
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(ListeningHistory.played_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().unique().all())


def add_listening_time(db: Session, account_id: int, seconds: int) -> None:
    safe_seconds = max(0, min(int(seconds), 300))
    if not safe_seconds:
        return
    db.execute(
        update(User)
        .where(User.id == account_id)
        .values(total_listening_seconds=User.total_listening_seconds + safe_seconds)
    )
    db.commit()


def get_history_summary(db: Session, user_id: str = DEFAULT_USER_ID, account_id: int | None = None) -> dict[str, int]:
    total_tracks = db.execute(
        select(func.count(ListeningHistory.id))
        .select_from(ListeningHistory)
        .where(ListeningHistory.user_id == user_id)
    ).scalar_one()
    total_seconds = 0
    if account_id is not None:
        total_seconds = db.execute(
            select(User.total_listening_seconds).where(User.id == account_id)
        ).scalar_one_or_none() or 0
    return {
        "total_seconds": max(0, int(total_seconds or 0)),
        "total_tracks": max(0, int(total_tracks or 0)),
    }
