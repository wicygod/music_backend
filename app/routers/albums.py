from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.albums import get_album
from app.schemas.album import AlbumRead
from app.services.serialization_service import album_to_read


router = APIRouter(prefix="/api/albums", tags=["albums"])


@router.get("/{album_id}", response_model=AlbumRead)
def read_album(album_id: int, db: Session = Depends(get_db)) -> AlbumRead:
    album = get_album(db, album_id)
    if album is None or not album.is_available:
        raise HTTPException(status_code=404, detail="Album not found")
    return album_to_read(album)
