from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.albums import album_refresh_due, search_album_matches
from app.repositories.artists import get_artists_by_ids
from app.schemas.album import SearchOverview
from app.schemas.track import TrackRead
from app.services.search_service import search_hydration_pending, search_local_catalog
from app.services.album_import_service import album_hydration_pending, schedule_artist_album_hydration
from app.services.serialization_service import album_to_read


router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=list[TrackRead])
def search(
    background_tasks: BackgroundTasks,
    q: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(150, ge=1, le=150),
    db: Session = Depends(get_db),
) -> list[TrackRead]:
    return search_local_catalog(db, q, background_tasks, limit=limit)


@router.get("/search/overview", response_model=SearchOverview)
def search_overview(
    background_tasks: BackgroundTasks,
    q: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(150, ge=1, le=150),
    album_limit: int = Query(3, ge=1, le=10),
    db: Session = Depends(get_db),
) -> SearchOverview:
    tracks = search_local_catalog(db, q, background_tasks, limit=limit)
    album_matches = search_album_matches(db, q, limit=album_limit)
    candidate_ids = list(dict.fromkeys(
        track.artists[0].id
        for track in tracks[:8]
        if track.artists
    ))
    artists_by_id = {artist.id: artist for artist in get_artists_by_ids(db, candidate_ids)}
    scheduled = 0
    for artist_id in candidate_ids:
        artist = artists_by_id.get(artist_id)
        if (
            artist is None
            or not artist.is_canonical
            or (artist.source_name or "").lower() not in {"soundcloud", "sc"}
            or not album_refresh_due(db, artist.id)
        ):
            continue
        if schedule_artist_album_hydration(artist.id, background_tasks):
            scheduled += 1
        if scheduled >= 2:
            break
    return SearchOverview(
        tracks=tracks,
        albums=[album_to_read(album, matched_track_id) for album, matched_track_id in album_matches],
        refresh_pending=(
            search_hydration_pending(q)
            or album_hydration_pending(candidate_ids)
        ),
    )
