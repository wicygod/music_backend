from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.artists import get_artist, get_artist_track_count, get_artist_tracks, list_artists
from app.schemas.artist import ArtistListResponse, ArtistWithTracks
from app.schemas.track import TrackRead
from app.services.serialization_service import artist_to_read, track_to_read


router = APIRouter(prefix="/api/artists", tags=["artists"])


@router.get("", response_model=ArtistListResponse)
def read_artists(
    q: str | None = Query(None),
    region: str | None = Query(None),
    priority: str | None = Query(None),
    needs_review: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ArtistListResponse:
    artists, total = list_artists(
        db,
        q=q,
        region=region,
        priority=priority,
        needs_review=needs_review,
        limit=limit,
        offset=offset,
    )
    return ArtistListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[artist_to_read(artist) for artist in artists],
    )


@router.get("/{artist_id}", response_model=ArtistWithTracks)
def read_artist(artist_id: int, db: Session = Depends(get_db)) -> ArtistWithTracks:
    artist = get_artist(db, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return artist_to_read(artist, get_artist_track_count(db, artist_id))


@router.get("/{artist_id}/tracks", response_model=list[TrackRead])
def read_artist_tracks(artist_id: int, db: Session = Depends(get_db)) -> list[TrackRead]:
    artist = get_artist(db, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return [track_to_read(track) for track in get_artist_tracks(db, artist_id)]
