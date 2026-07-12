import secrets
import shutil
import string
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import ADMIN_API_KEY, token_matches
from app.database import get_db
from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.playlist import UserFavorite, UserPlaylist, UserPlaylistTrack
from app.models.track import Track, TrackArtist
from app.models.user import BlockedUser, User
from app.schemas.auth import BanRequest
from app.services.admin_monitor import activity_snapshot, recent_events, record_event, system_stats
from app.services.auth_service import hash_password, user_to_read
from app.services.serialization_service import track_to_read


router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminUserUpdate(BaseModel):
    nickname: str | None = Field(default=None, min_length=2, max_length=96)
    avatar_url: str | None = Field(default=None, max_length=2_000_000)
    subscription_status: str | None = Field(default=None, max_length=32)


class PasswordResetResponse(BaseModel):
    ok: bool
    temporary_password: str
    user: dict


class AudioCachePruneRequest(BaseModel):
    max_age_hours: int = Field(default=24, ge=1, le=24 * 30)


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


def _user_scope(user_id: int) -> str:
    return f"account:{user_id}"


def _generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "MD-" + "".join(secrets.choice(alphabet) for _ in range(length))


def _user_metrics(db: Session, user_id: int) -> dict:
    scope = _user_scope(user_id)
    playlist_ids = db.execute(select(UserPlaylist.id).where(UserPlaylist.user_id == scope)).scalars().all()
    playlist_track_count = 0
    if playlist_ids:
        playlist_track_count = int(
            db.execute(
                select(func.count(UserPlaylistTrack.track_id)).where(UserPlaylistTrack.playlist_id.in_(playlist_ids))
            ).scalar() or 0
        )
    return {
        "history_count": int(
            db.execute(select(func.count(ListeningHistory.id)).where(ListeningHistory.user_id == scope)).scalar() or 0
        ),
        "favorites_count": int(
            db.execute(select(func.count(UserFavorite.track_id)).where(UserFavorite.user_id == scope)).scalar() or 0
        ),
        "playlists_count": len(playlist_ids),
        "playlist_tracks_count": playlist_track_count,
    }


def _user_payload(db: Session, user: User) -> dict:
    payload = user_to_read(user).model_dump(mode="json")
    payload["metrics"] = _user_metrics(db, user.id)
    return payload


def _catalog_metrics(db: Session) -> dict:
    total_tracks = int(db.execute(select(func.count(Track.id))).scalar() or 0)
    total_duration = int(db.execute(select(func.sum(Track.duration_seconds))).scalar() or 0)
    source_rows = db.execute(
        select(Track.source_name, func.count(Track.id).label("track_count"))
        .group_by(Track.source_name)
        .order_by(desc("track_count"))
        .limit(5)
    ).all()
    return {
        "tracks": total_tracks,
        "artists": int(db.execute(select(func.count(Artist.id))).scalar() or 0),
        "playable_tracks": int(
            db.execute(select(func.count(Track.id)).where(Track.is_playable.is_(True))).scalar() or 0
        ),
        "needs_review": int(
            db.execute(select(func.count(Track.id)).where(Track.needs_review.is_(True))).scalar() or 0
        ),
        "missing_covers": int(
            db.execute(
                select(func.count(Track.id)).where(
                    or_(Track.cover_url.is_(None), func.trim(Track.cover_url) == "")
                )
            ).scalar()
            or 0
        ),
        "duration_seconds": total_duration,
        "sources": [
            {"name": source_name or "unknown", "tracks": int(track_count)}
            for source_name, track_count in source_rows
        ],
    }


def _community_metrics(db: Session) -> dict:
    week_ago = datetime.utcnow() - timedelta(days=7)
    return {
        "users": int(db.execute(select(func.count(User.id))).scalar() or 0),
        "new_users_7d": int(
            db.execute(select(func.count(User.id)).where(User.created_at >= week_ago)).scalar() or 0
        ),
        "subscribed_users": int(
            db.execute(
                select(func.count(User.id)).where(
                    func.coalesce(User.subscription_status, "inactive") != "inactive"
                )
            ).scalar()
            or 0
        ),
        "favorites": int(db.execute(select(func.count(UserFavorite.track_id))).scalar() or 0),
        "playlists": int(db.execute(select(func.count(UserPlaylist.id))).scalar() or 0),
        "playlist_tracks": int(db.execute(select(func.count(UserPlaylistTrack.track_id))).scalar() or 0),
    }


def _audio_cache_settings() -> tuple[Path, int]:
    # Import lazily to keep the monitor independent from the streaming router at startup.
    from app.routers.stream import AUDIO_CACHE_DIR, AUDIO_CACHE_MAX_BYTES

    return Path(AUDIO_CACHE_DIR), int(AUDIO_CACHE_MAX_BYTES)


def _audio_cache_overview() -> dict:
    directory, max_bytes = _audio_cache_settings()
    completed: list[tuple[Path, int, float]] = []
    building = 0
    try:
        for item in directory.iterdir() if directory.is_dir() else ():
            if not item.is_file():
                continue
            if item.suffix == ".part":
                building += 1
                continue
            if item.suffix != ".mp3":
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            completed.append((item, int(stat.st_size), float(stat.st_mtime)))
    except OSError:
        completed = []

    total_bytes = sum(size for _, size, _ in completed)
    oldest = min((modified for _, _, modified in completed), default=None)
    newest = max((modified for _, _, modified in completed), default=None)
    stale_cutoff = time.time() - 24 * 60 * 60
    disk_probe = directory if directory.exists() else directory.parent
    try:
        disk_free_bytes = int(shutil.disk_usage(disk_probe).free)
    except OSError:
        disk_free_bytes = 0

    def as_iso(timestamp: float | None) -> str | None:
        return datetime.utcfromtimestamp(timestamp).isoformat(timespec="seconds") + "Z" if timestamp else None

    return {
        "directory": str(directory),
        "files": len(completed),
        "building": building,
        "bytes": total_bytes,
        "max_bytes": max_bytes,
        "usage_percent": round((total_bytes / max_bytes) * 100, 1) if max_bytes else 0,
        "disk_free_bytes": disk_free_bytes,
        "stale_files_24h": sum(modified < stale_cutoff for _, _, modified in completed),
        "oldest_at": as_iso(oldest),
        "newest_at": as_iso(newest),
    }


def _prune_audio_cache(max_age_hours: int) -> tuple[int, int]:
    directory, _ = _audio_cache_settings()
    cutoff = time.time() - max_age_hours * 60 * 60
    removed_files = 0
    freed_bytes = 0
    try:
        entries = tuple(directory.glob("*.mp3")) if directory.is_dir() else ()
    except OSError:
        entries = ()
    for item in entries:
        try:
            stat = item.stat()
            if stat.st_mtime >= cutoff:
                continue
            item.unlink()
        except OSError:
            continue
        removed_files += 1
        freed_bytes += int(stat.st_size)
    return removed_files, freed_bytes


@router.get("/stats", dependencies=[Depends(require_admin_key)])
def admin_stats(db: Session = Depends(get_db)) -> dict:
    stats = system_stats()
    stats["top_tracks"] = _top_tracks(db, limit=10)
    stats["total_users"] = int(db.execute(select(func.count(User.id))).scalar() or 0)
    stats["total_plays"] = int(db.execute(select(func.count(ListeningHistory.id))).scalar() or 0)
    stats["banned_users"] = int(db.execute(select(func.count(BlockedUser.id))).scalar() or 0)
    return stats


@router.get("/logs", dependencies=[Depends(require_admin_key)])
def admin_logs(limit: int = Query(80, ge=1, le=300)) -> dict:
    return {"events": recent_events(limit=limit)}


@router.get("/overview", dependencies=[Depends(require_admin_key)])
def admin_overview(db: Session = Depends(get_db)) -> dict:
    return {
        "catalog": _catalog_metrics(db),
        "community": _community_metrics(db),
        "activity": activity_snapshot(window_seconds=60 * 60, recent_limit=8),
        "audio_cache": _audio_cache_overview(),
    }


@router.post("/cache/audio/prune", dependencies=[Depends(require_admin_key)])
def prune_audio_cache(payload: AudioCachePruneRequest) -> dict:
    removed_files, freed_bytes = _prune_audio_cache(payload.max_age_hours)
    record_event(
        "admin",
        f"Admin pruned {removed_files} inactive audio cache file(s)",
        path="/api/admin/cache/audio/prune",
    )
    return {
        "ok": True,
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "audio_cache": _audio_cache_overview(),
    }


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
    return {"users": [_user_payload(db, user) for user in users]}


@router.get("/users/{user_id}", dependencies=[Depends(require_admin_key)])
def admin_user_detail(user_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    history_stmt = (
        select(Track, ListeningHistory.played_at)
        .join(ListeningHistory, ListeningHistory.track_id == Track.id)
        .where(ListeningHistory.user_id == _user_scope(user_id))
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(ListeningHistory.played_at.desc())
        .limit(20)
    )
    history = [
        {"track": track_to_read(track).model_dump(mode="json"), "played_at": played_at.isoformat()}
        for track, played_at in db.execute(history_stmt).all()
    ]
    return {"user": _user_payload(db, user), "history": history}


@router.post("/users/{user_id}/ban", dependencies=[Depends(require_admin_key)])
def ban_user(user_id: int, payload: BanRequest | None = None, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.block:
        db.add(BlockedUser(user_id=user.id, reason=(payload.reason if payload else None) or "manual admin ban"))
        db.commit()
        db.refresh(user)
    record_event("admin", f"Admin banned @{user.login}", path=f"/api/admin/users/{user_id}/ban")
    return {"ok": True, "user": _user_payload(db, user)}


@router.post("/users/{user_id}/unban", dependencies=[Depends(require_admin_key)])
def unban_user(user_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.block:
        db.delete(user.block)
        db.commit()
        db.refresh(user)
    record_event("admin", f"Admin unbanned @{user.login}", path=f"/api/admin/users/{user_id}/unban")
    return {"ok": True, "user": _user_payload(db, user)}


@router.patch("/users/{user_id}", dependencies=[Depends(require_admin_key)])
def update_user(user_id: int, payload: AdminUserUpdate, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.nickname is not None:
        user.nickname = payload.nickname.strip()
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url.strip() or None
    if payload.subscription_status is not None:
        user.subscription_status = payload.subscription_status.strip() or "inactive"
    db.add(user)
    db.commit()
    db.refresh(user)
    record_event("admin", f"Admin updated @{user.login}", path=f"/api/admin/users/{user_id}")
    return {"ok": True, "user": _user_payload(db, user)}


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(require_admin_key)])
def reset_user_password(user_id: int, db: Session = Depends(get_db)) -> PasswordResetResponse:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    temporary_password = _generate_temp_password()
    user.password_hash = hash_password(temporary_password)
    db.add(user)
    db.commit()
    db.refresh(user)
    record_event("admin", f"Admin generated temporary password for @{user.login}", path=f"/api/admin/users/{user_id}/reset-password")
    return PasswordResetResponse(ok=True, temporary_password=temporary_password, user=_user_payload(db, user))


@router.post("/users/{user_id}/clear-data", dependencies=[Depends(require_admin_key)])
def clear_user_data(user_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.execute(select(User).where(User.id == user_id).options(selectinload(User.block))).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    scope = _user_scope(user_id)
    playlist_ids = db.execute(select(UserPlaylist.id).where(UserPlaylist.user_id == scope)).scalars().all()
    if playlist_ids:
        db.query(UserPlaylistTrack).filter(UserPlaylistTrack.playlist_id.in_(playlist_ids)).delete(synchronize_session=False)
    db.query(UserPlaylist).filter(UserPlaylist.user_id == scope).delete(synchronize_session=False)
    db.query(UserFavorite).filter(UserFavorite.user_id == scope).delete(synchronize_session=False)
    db.query(ListeningHistory).filter(ListeningHistory.user_id == scope).delete(synchronize_session=False)
    db.commit()
    record_event("admin", f"Admin cleared data for @{user.login}", path=f"/api/admin/users/{user_id}/clear-data")
    return {"ok": True, "user": _user_payload(db, user)}
