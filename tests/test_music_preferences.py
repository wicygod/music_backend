import os
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.models.artist import Artist
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.routers.personalization import create_music_signal, update_music_preferences
from app.schemas.personalization import MusicPreferencesUpdate, MusicSignalCreate
from app.services.preference_service import PreferenceServiceError, save_music_preferences
from app.repositories.personalization import apply_preference_signal
from app.services.normalization_service import normalize_artist_name


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _artists(db: Session, count: int = 4) -> list[Artist]:
    result = []
    for index in range(count):
        name = f"Preference Artist {index + 1}"
        artist = Artist(
            name=name,
            normalized_name=normalize_artist_name(name),
            region="global",
            needs_review=False,
        )
        db.add(artist)
        result.append(artist)
    db.flush()
    return result


def _request_for(user_id: int | None) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/api/user/music-preferences", "headers": []})
    if user_id is not None:
        request.state.user_id = user_id
    return request


def test_initial_save_deduplicates_before_minimum_and_retry_is_idempotent() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="preference-save", nickname="Listener", password_hash="hash")
        db.add(user)
        artists = _artists(db)
        db.commit()

        with pytest.raises(PreferenceServiceError) as error:
            save_music_preferences(
                db,
                user_id=user.id,
                artist_ids=[artists[0].id, artists[0].id, artists[1].id],
            )
        assert error.value.code == "not_enough_artists"

        first = save_music_preferences(
            db,
            user_id=user.id,
            artist_ids=[artists[0].id, artists[0].id, artists[1].id, artists[2].id],
        )
        second = save_music_preferences(
            db,
            user_id=user.id,
            artist_ids=[artists[0].id, artists[1].id, artists[2].id],
        )

        assert first.selected_artist_ids == [artists[0].id, artists[1].id, artists[2].id]
        assert first.added_artist_ids == first.selected_artist_ids
        assert second.added_artist_ids == []
        assert second.removed_artist_ids == []
        assert second.completed_at == first.completed_at
        stored = list(
            db.execute(
                select(UserArtistPreference).where(UserArtistPreference.user_id == user.id)
            ).scalars().all()
        )
        assert len(stored) == 3
        assert all(item.explicit_weight == 5.0 and item.weight == 5.0 for item in stored)

    engine.dispose()


def test_skip_is_repeatable_and_unknown_artist_is_rejected() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="preference-skip", nickname="Listener", password_hash="hash")
        db.add(user)
        db.commit()

        first = save_music_preferences(db, user_id=user.id, artist_ids=[], skipped=True)
        second = save_music_preferences(db, user_id=user.id, artist_ids=[], skipped=True)

        assert first.skipped is second.skipped is True
        assert first.completed_at == second.completed_at
        assert first.selected_artist_ids == second.selected_artist_ids == []

        another = User(login="unknown-artist", nickname="Listener", password_hash="hash")
        db.add(another)
        db.commit()
        with pytest.raises(PreferenceServiceError) as error:
            save_music_preferences(db, user_id=another.id, artist_ids=[999_999, 999_998, 999_997])
        assert error.value.code == "unknown_artists"

    engine.dispose()


def test_settings_removal_preserves_behavioral_interest() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="settings-removal", nickname="Listener", password_hash="hash")
        db.add(user)
        artists = _artists(db)
        db.commit()
        save_music_preferences(db, user_id=user.id, artist_ids=[item.id for item in artists[:3]])
        apply_preference_signal(
            db,
            user_id=user.id,
            artist_id=artists[0].id,
            source="like",
            delta=4.0,
        )
        db.commit()

        result = save_music_preferences(
            db,
            user_id=user.id,
            artist_ids=[artists[1].id],
            source="settings",
        )
        removed = db.execute(
            select(UserArtistPreference).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artists[0].id,
            )
        ).scalar_one()

        assert result.removed_artist_ids == [artists[0].id, artists[2].id]
        assert removed.explicit_selected is False
        assert removed.explicit_source is None
        assert removed.explicit_weight == 0.0
        assert removed.behavior_weight == pytest.approx(4.0, abs=0.01)
        assert removed.weight == pytest.approx(4.0, abs=0.01)

    engine.dispose()


def test_preference_route_uses_authenticated_state_and_cannot_target_another_user() -> None:
    engine = _engine()
    with Session(engine) as db:
        owner = User(login="route-owner", nickname="Owner", password_hash="hash")
        victim = User(login="route-victim", nickname="Victim", password_hash="hash")
        db.add_all([owner, victim])
        artists = _artists(db, 3)
        db.commit()

        payload = MusicPreferencesUpdate.model_validate(
            {
                "artistIds": [artist.id for artist in artists],
                "source": "onboarding",
                "userId": victim.id,
            }
        )
        response = update_music_preferences(payload, _request_for(owner.id), db)

        assert response.selected_artist_ids == [artist.id for artist in artists]
        assert db.execute(
            select(UserArtistPreference).where(UserArtistPreference.user_id == owner.id)
        ).scalars().all()
        assert db.execute(
            select(UserArtistPreference).where(UserArtistPreference.user_id == victim.id)
        ).scalars().all() == []

        with pytest.raises(HTTPException) as unauthorized:
            update_music_preferences(payload, _request_for(None), db)
        assert unauthorized.value.status_code == 401

    engine.dispose()


def test_music_signal_route_is_idempotent_and_scoped_to_authenticated_user() -> None:
    engine = _engine()
    with Session(engine) as db:
        owner = User(login="signal-owner", nickname="Owner", password_hash="hash")
        victim = User(login="signal-victim", nickname="Victim", password_hash="hash")
        db.add_all([owner, victim])
        artist = _artists(db, 1)[0]
        track = Track(
            title="Signal Song",
            normalized_title="signal song",
            duration_seconds=180,
            genre="Rock",
            quality_score=100,
            is_playable=True,
            source_name="soundcloud",
            source_url="https://soundcloud.com/test/signal-song",
            needs_review=False,
        )
        db.add(track)
        db.flush()
        db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
        db.commit()

        payload = MusicSignalCreate(
            event_id="music-signal-event-0001",
            signal="like",
            track_id=track.id,
            artist_id=artist.id,
            context="artist",
            occurred_at=datetime.utcnow(),
        )
        first = create_music_signal(payload, _request_for(owner.id), db)
        second = create_music_signal(payload, _request_for(owner.id), db)

        assert first.created is True
        assert second.created is False
        owner_preference = db.execute(
            select(UserArtistPreference).where(UserArtistPreference.user_id == owner.id)
        ).scalar_one()
        assert owner_preference.artist_id == artist.id
        assert owner_preference.behavior_weight == pytest.approx(4.0, abs=0.01)
        assert db.execute(
            select(UserArtistPreference).where(UserArtistPreference.user_id == victim.id)
        ).scalars().all() == []

    engine.dispose()
