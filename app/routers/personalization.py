from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.repositories.personalization import list_user_artist_preferences
from app.schemas.personalization import (
    MusicPreferencesRead,
    MusicPreferencesUpdate,
    MusicSignalCreate,
    MusicSignalRead,
    UserArtistPreferenceRead,
)
from app.services.admin_monitor import record_event
from app.services.preference_service import (
    PreferenceServiceError,
    record_music_signal,
    save_music_preferences,
)


router = APIRouter(prefix="/api/user", tags=["personalization"])


def _account_id(request: Request) -> int:
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing auth user")
    return int(user_id)


def _preference_read(item) -> UserArtistPreferenceRead:
    return UserArtistPreferenceRead(
        artist_id=item.artist_id,
        source=item.source,
        explicit_selected=item.explicit_selected,
        is_hidden=item.is_hidden,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/music-preferences", response_model=MusicPreferencesRead)
def read_music_preferences(
    request: Request,
    db: Session = Depends(get_db),
) -> MusicPreferencesRead:
    account_id = _account_id(request)
    user = db.get(User, account_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    items = list_user_artist_preferences(db, user_id=account_id)
    return MusicPreferencesRead(
        completed_at=user.music_preferences_completed_at,
        selected_artist_ids=[item.artist_id for item in items if item.explicit_selected],
        items=[_preference_read(item) for item in items],
    )


@router.post("/music-preferences", response_model=MusicPreferencesRead)
def update_music_preferences(
    payload: MusicPreferencesUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> MusicPreferencesRead:
    account_id = _account_id(request)
    try:
        result = save_music_preferences(
            db,
            user_id=account_id,
            artist_ids=payload.artist_ids,
            source=payload.source,
            skipped=payload.skipped,
        )
    except PreferenceServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    for artist_id in result.added_artist_ids:
        record_event("artist_selected", f"Artist selected: {artist_id}", path="/api/user/music-preferences")
    for artist_id in result.removed_artist_ids:
        record_event("artist_unselected", f"Artist unselected: {artist_id}", path="/api/user/music-preferences")
    record_event(
        "artist_onboarding_skipped" if result.skipped else "artist_onboarding_completed",
        "Music preference onboarding skipped" if result.skipped else "Music preferences saved",
        path="/api/user/music-preferences",
    )
    return MusicPreferencesRead(
        completed_at=result.completed_at,
        selected_artist_ids=result.selected_artist_ids,
        items=[_preference_read(item) for item in result.preferences],
    )


@router.post("/music-signals", response_model=MusicSignalRead)
def create_music_signal(
    payload: MusicSignalCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> MusicSignalRead:
    account_id = _account_id(request)
    try:
        result = record_music_signal(
            db,
            user_id=account_id,
            event_id=payload.event_id,
            track_id=payload.track_id,
            artist_id=payload.artist_id,
            event_type=f"music_{payload.signal}",
            recommendation_type="behavioral_signal",
            context=payload.context,
            signal_type=payload.signal,
            occurred_at=payload.occurred_at,
        )
    except PreferenceServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return MusicSignalRead(
        event_id=result.event.event_id,
        signal=payload.signal,
        created=result.created,
    )
