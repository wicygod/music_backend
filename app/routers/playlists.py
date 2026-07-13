from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
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


router = APIRouter(prefix="/api/user", tags=["user"])


def _account_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return f"account:{int(user_id)}"


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
    playlist = add_track_to_playlist(
        db,
        playlist_id=playlist_id,
        track_id=track_id,
        user_id=_account_user_id(request),
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist or track not found")
    return playlist_to_read(playlist)


@router.delete("/playlists/{playlist_id}/tracks/{track_id}", response_model=PlaylistRead)
def delete_playlist_track(
    playlist_id: int,
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> PlaylistRead:
    playlist = remove_track_from_playlist(
        db,
        playlist_id=playlist_id,
        track_id=track_id,
        user_id=_account_user_id(request),
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist_to_read(playlist)


@router.post("/favorites/{track_id}", response_model=FavoriteRead, status_code=201)
def add_user_favorite(
    track_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> FavoriteRead:
    favorite = add_favorite(db, track_id=track_id, user_id=_account_user_id(request))
    if not favorite:
        raise HTTPException(status_code=404, detail="Track not found")
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
    remove_favorite(db, track_id=track_id, user_id=_account_user_id(request))
    return Response(status_code=204)
