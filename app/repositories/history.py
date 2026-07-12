from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.history import ListeningHistory
from app.models.track import Track, TrackArtist
from app.repositories.tracks import get_track


DEFAULT_USER_ID = "local"
HISTORY_LIMIT_PER_USER = 50


def trim_user_history(db: Session, user_id: str, keep: int = HISTORY_LIMIT_PER_USER) -> None:
    old_ids = db.execute(
        select(ListeningHistory.id)
        .where(ListeningHistory.user_id == user_id)
        .order_by(ListeningHistory.played_at.desc(), ListeningHistory.id.desc())
        .offset(keep)
    ).scalars().all()
    if old_ids:
        db.execute(delete(ListeningHistory).where(ListeningHistory.id.in_(old_ids)))


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
    else:
        db.add(ListeningHistory(user_id=user_id, track_id=track_id, played_at=datetime.utcnow()))

    db.flush()
    trim_user_history(db, user_id=user_id)
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


def get_history_summary(db: Session, user_id: str = DEFAULT_USER_ID) -> dict[str, int]:
    total_seconds, total_tracks = db.execute(
        select(
            func.coalesce(func.sum(Track.duration_seconds), 0),
            func.count(ListeningHistory.id),
        )
        .select_from(ListeningHistory)
        .join(Track, ListeningHistory.track_id == Track.id)
        .where(ListeningHistory.user_id == user_id)
    ).one()
    return {
        "total_seconds": max(0, int(total_seconds or 0)),
        "total_tracks": max(0, int(total_tracks or 0)),
    }
