import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.artists import get_artist, get_artist_track_count, get_artist_tracks, list_artists
from app.repositories.personalization import list_onboarding_artists
from app.schemas.artist import ArtistListResponse, ArtistWithTracks
from app.schemas.personalization import OnboardingArtistRead, OnboardingArtistsResponse
from app.schemas.track import TrackRead
from app.services.admin_monitor import record_event
from app.services.canonical_artist_service import refresh_canonical_artist_for_search
from app.services.recommendation_config import RECOMMENDATION_CONFIG
from app.services.serialization_service import artist_to_read, track_to_read


router = APIRouter(prefix="/api/artists", tags=["artists"])
logger = logging.getLogger(__name__)


@router.get("/onboarding", response_model=OnboardingArtistsResponse)
def read_onboarding_artists(
    request: Request,
    search: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    limit: int = Query(RECOMMENDATION_CONFIG.onboarding_page_size, ge=1, le=100),
    genre: str | None = Query(None, max_length=64),
    db: Session = Depends(get_db),
) -> OnboardingArtistsResponse:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    if search and page == 1:
        try:
            refresh_canonical_artist_for_search(db, search)
            db.commit()
        except Exception:  # noqa: BLE001 - provider availability must not break local search
            db.rollback()
            logger.exception("Canonical artist refresh failed")
    items, total = list_onboarding_artists(
        db,
        user_id=int(user_id),
        search=search,
        page=page,
        limit=limit,
        genre=genre,
    )
    if page == 1 and not search and not genre:
        record_event(
            "artist_onboarding_opened",
            "Artist preference onboarding opened",
            path="/api/artists/onboarding",
        )
    return OnboardingArtistsResponse(
        items=[
            OnboardingArtistRead(
                id=item.id,
                name=item.name,
                avatar_url=item.avatar_url,
                genres=item.genres,
                popularity_score=item.popularity_score,
                track_count=item.track_count,
                selected=item.selected,
            )
            for item in items
        ],
        total=total,
        page=page,
        limit=limit,
        has_more=page * limit < total,
        minimum_required=RECOMMENDATION_CONFIG.minimum_onboarding_artists,
    )


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
