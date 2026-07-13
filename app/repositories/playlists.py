from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.playlist import UserFavorite, UserPlaylist, UserPlaylistTrack
from app.models.track import Track, TrackArtist


def playlist_options(stmt):
    return stmt.options(
        selectinload(UserPlaylist.track_links)
        .selectinload(UserPlaylistTrack.track)
        .selectinload(Track.artist_links)
        .selectinload(TrackArtist.artist)
    )


def get_playlist(db: Session, playlist_id: int, *, user_id: str | None = None) -> UserPlaylist | None:
    stmt = select(UserPlaylist).where(UserPlaylist.id == playlist_id)
    if user_id is not None:
        stmt = stmt.where(UserPlaylist.user_id == user_id)
    stmt = playlist_options(stmt)
    return db.execute(stmt).scalars().unique().first()


def list_playlists(db: Session, user_id: str = "local-user") -> list[UserPlaylist]:
    stmt = playlist_options(
        select(UserPlaylist).where(UserPlaylist.user_id == user_id).order_by(UserPlaylist.created_at.desc())
    )
    return list(db.execute(stmt).scalars().unique().all())


def create_playlist(
    db: Session,
    *,
    name: str,
    description: str | None = None,
    user_id: str = "local-user",
) -> UserPlaylist:
    playlist = UserPlaylist(user_id=user_id, name=name.strip(), description=description)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return get_playlist(db, playlist.id, user_id=user_id) or playlist


def add_track_to_playlist(
    db: Session,
    *,
    playlist_id: int,
    track_id: int,
    user_id: str,
) -> UserPlaylist | None:
    playlist = get_playlist(db, playlist_id, user_id=user_id)
    if not playlist or not db.get(Track, track_id):
        return None
    existing = db.get(UserPlaylistTrack, {"playlist_id": playlist_id, "track_id": track_id})
    if not existing:
        db.add(UserPlaylistTrack(playlist_id=playlist_id, track_id=track_id))
        db.commit()
    return get_playlist(db, playlist_id, user_id=user_id)


def remove_track_from_playlist(
    db: Session,
    *,
    playlist_id: int,
    track_id: int,
    user_id: str,
) -> UserPlaylist | None:
    playlist = get_playlist(db, playlist_id, user_id=user_id)
    if not playlist:
        return None
    existing = db.get(UserPlaylistTrack, {"playlist_id": playlist_id, "track_id": track_id})
    if existing:
        db.delete(existing)
        db.commit()
    return get_playlist(db, playlist_id, user_id=user_id)


def add_favorite(db: Session, *, track_id: int, user_id: str = "local-user") -> UserFavorite | None:
    track = db.get(Track, track_id)
    if not track:
        return None
    favorite = db.get(UserFavorite, {"user_id": user_id, "track_id": track_id})
    if not favorite:
        favorite = UserFavorite(user_id=user_id, track_id=track_id)
        db.add(favorite)
        db.commit()
    stmt = (
        select(UserFavorite)
        .where(UserFavorite.user_id == user_id, UserFavorite.track_id == track_id)
        .options(selectinload(UserFavorite.track).selectinload(Track.artist_links).selectinload(TrackArtist.artist))
    )
    return db.execute(stmt).scalars().unique().first()


def list_favorites(db: Session, *, user_id: str) -> list[UserFavorite]:
    stmt = (
        select(UserFavorite)
        .where(UserFavorite.user_id == user_id)
        .options(selectinload(UserFavorite.track).selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(UserFavorite.created_at.desc())
    )
    return list(db.execute(stmt).scalars().unique().all())


def remove_favorite(db: Session, *, track_id: int, user_id: str = "local-user") -> bool:
    favorite = db.get(UserFavorite, {"user_id": user_id, "track_id": track_id})
    if not favorite:
        return False
    db.delete(favorite)
    db.commit()
    return True
