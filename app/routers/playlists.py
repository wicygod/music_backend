from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.playlist import UserFavorite, UserPlaylistTrack
from app.repositories.playlists import (
    add_favorite,
    add_track_to_playlist,
    create_playlist,
    list_favorites,
    list_playlists,
    remove_favorite,
    remove_track_from_playlist,
)
from app.schemas.playlist import FavoriteRead, PlaylistCreate, PlaylistRead
from app.services.serialization_service import favorite_to_read, playlist_to_read
from app.services.admin_monitor import record_event
from app.services.preference_service import PreferenceServiceError, record_music_signal


router = APIRouter(prefix="/api/user", tags=["user"])


def _account_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return f"account:{int(user_id)}"


def _account_id(request: Request) -> int:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return int(user_id)


def _record_library_signal(
    db: Session,
    *,
    account_id: int,
    track_id: int,
    signal: str,
    context: str,
) -> None:
    try:
        result = record_music_signal(
            db,
            user_id=account_id,
            event_id=f"{signal}:{uuid4().hex}",
            track_id=track_id,
            event_type="recommendation_liked" if signal == "like" else f"library_{signal}",
            recommendation_type="library",
            context=context,
            signal_type=signal,
        )
        if result.created and signal == "like":
            record_event(
                "recommendation_liked",
                f"Recommendation liked for track {track_id}",
                path="/api/user/favorites",
            )
    except PreferenceServiceError as exc:
        record_event("error", f"Preference signal failed: {exc.code}", path="/api/user")


@router.post("/playlists", response_model=PlaylistRead, status_code=201)
def create_user_playlist(
    payload: PlaylistCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> PlaylistRead:
    playlist = create_playlist(
        db,
        name=payload.name,
        description=payload.description,
        user_id=_account_user_id(request),
    )
    return playlist_to_read(playlist)


@router.get("/playlists", response_model=list[PlaylistRead])
def read_user_playlists(
    request: Request,
    db: Session = Depends(get_db),
) -> list[PlaylistRead]:
    return [playlist_to_read(playlist) for playlist in list_playlists(db, user_id=_account_user_id(request))]


@router.post("/playlists/{playlist_id}/tracks/{track_id}", response_model=PlaylistRead)
def add_playlist_track(
    playlist_id: int,
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> PlaylistRead:
    account_id = _account_id(request)
    existed = db.get(UserPlaylistTrack, {"playlist_id": playlist_id, "track_id": track_id}) is not None
    playlist = add_track_to_playlist(
        db,
        playlist_id=playlist_id,
        track_id=track_id,
        user_id=_account_user_id(request),
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist or track not found")
    if not existed:
        _record_library_signal(
            db,
            account_id=account_id,
            track_id=track_id,
            signal="playlist",
            context="playlist",
        )
    return playlist_to_read(playlist)


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", response_model=PlaylistRead)
def delete_playlist_track(
    playlist_id: int,
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> PlaylistRead:
    account_id = _account_id(request)
    existed = db.get(UserPlaylistTrack, {"playlist_id": playlist_id, "track_id": track_id}) is not None
    playlist = remove_track_from_playlist(
        db,
        playlist_id=playlist_id,
        track_id=track_id,
        user_id=_account_user_id(request),
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if existed:
        _record_library_signal(
            db,
            account_id=account_id,
            track_id=track_id,
            signal="playlist_remove",
            context="playlist",
        )
    return playlist_to_read(playlist)


@router.post("/favorites/{track_id}", response_model=FavoriteRead, status_code=201)
def add_user_favorite(
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> FavoriteRead:
    account_id = _account_id(request)
    scope = _account_user_id(request)
    existed = db.get(UserFavorite, {"user_id": scope, "track_id": track_id}) is not None
    favorite = add_favorite(db, track_id=track_id, user_id=scope)
    if not favorite:
        raise HTTPException(status_code=404, detail="Track not found")
    if not existed:
        _record_library_signal(
            db,
            account_id=account_id,
            track_id=track_id,
            signal="like",
            context="favorites",
        )
    return favorite_to_read(favorite)


@router.get("/favorites", response_model=list[FavoriteRead])
def read_user_favorites(
    request: Request,
    db: Session = Depends(get_db),
) -> list[FavoriteRead]:
    return [
        favorite_to_read(favorite)
        for favorite in list_favorites(db, user_id=_account_user_id(request))
    ]


@router.delete("/favorites/{track_id}", status_code=204)
def delete_user_favorite(
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    account_id = _account_id(request)
    scope = _account_user_id(request)
    existed = db.get(UserFavorite, {"user_id": scope, "track_id": track_id}) is not None
    remove_favorite(db, track_id=track_id, user_id=scope)
    if existed:
        _record_library_signal(
            db,
            account_id=account_id,
            track_id=track_id,
            signal="unlike",
            context="favorites",
        )
    return Response(status_code=204)
