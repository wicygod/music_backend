import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.repositories.history import record_listening_event
from app.schemas.personalization import ListeningEventCreate
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.recommendation_config import RECOMMENDATION_CONFIG


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _catalog(db: Session, *, duration: int = 100, suffix: str = "one") -> tuple[User, Artist, Track]:
    user = User(login=f"history-{suffix}", nickname="Listener", password_hash="hash")
    artist_name = f"History Artist {suffix}"
    artist = Artist(
        name=artist_name,
        normalized_name=normalize_artist_name(artist_name),
        region="global",
        needs_review=False,
    )
    db.add_all([user, artist])
    db.flush()
    track = Track(
        title=f"History Song {suffix}",
        normalized_title=normalize_title(f"History Song {suffix}"),
        duration_seconds=duration,
        genre="Rock",
        popularity_score=65.0,
        quality_score=100.0,
        is_playable=True,
        source_name="soundcloud",
        source_url=f"https://soundcloud.com/test/history-{suffix}",
        needs_review=False,
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    db.commit()
    return user, artist, track


def _payload(
    event_id: str,
    track: Track,
    artist: Artist,
    listened: int,
    *,
    track_duration: int | None = None,
    completion_ratio: float | None = None,
) -> ListeningEventCreate:
    return ListeningEventCreate(
        event_id=event_id,
        track_id=track.id,
        artist_id=artist.id,
        started_at=datetime.utcnow(),
        listened_duration_seconds=listened,
        track_duration_seconds=track_duration,
        completion_ratio=completion_ratio,
        completed=False,
        skipped=False,
        context="home",
    )


@pytest.mark.parametrize(
    ("listened", "expected_completed", "expected_skipped", "expected_delta"),
    [
        (95, True, False, RECOMMENDATION_CONFIG.completed_play_weight),
        (75, False, False, RECOMMENDATION_CONFIG.substantial_play_weight),
        (5, False, True, RECOMMENDATION_CONFIG.quick_skip_weight),
    ],
)
def test_full_substantial_and_quick_skip_signals(
    listened: int,
    expected_completed: bool,
    expected_skipped: bool,
    expected_delta: float,
) -> None:
    engine = _engine()
    with Session(engine) as db:
        user, artist, track = _catalog(db, suffix=f"signal-{listened}")

        item, created = record_listening_event(
            db,
            account_id=user.id,
            payload=_payload(f"signal-event-{listened}", track, artist, listened),
        )
        preference = db.execute(
            select(UserArtistPreference).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()

        assert created is True
        assert item.completion_ratio == pytest.approx(listened / 100)
        assert item.completed is expected_completed
        assert item.skipped is expected_skipped
        assert preference.behavior_weight == pytest.approx(expected_delta, abs=0.01)

    engine.dispose()


def test_listening_event_retry_is_idempotent_for_history_and_preference() -> None:
    engine = _engine()
    with Session(engine) as db:
        user, artist, track = _catalog(db, suffix="retry")
        payload = _payload("retry-event-0001", track, artist, 95)

        first, first_created = record_listening_event(db, account_id=user.id, payload=payload)
        before = db.execute(
            select(UserArtistPreference.behavior_weight).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()
        second, second_created = record_listening_event(db, account_id=user.id, payload=payload)
        after = db.execute(
            select(UserArtistPreference.behavior_weight).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()

        assert first_created is True
        assert second_created is False
        assert first.id == second.id
        assert db.execute(
            select(func.count(ListeningHistory.id)).where(ListeningHistory.event_id == payload.event_id)
        ).scalar_one() == 1
        assert after == before

    engine.dispose()


def test_unknown_duration_uses_bounded_client_ratio() -> None:
    engine = _engine()
    with Session(engine) as db:
        user, artist, track = _catalog(db, duration=0, suffix="unknown-duration")
        payload = _payload(
            "unknown-duration-event",
            track,
            artist,
            30,
            track_duration=None,
            completion_ratio=0.75,
        )

        item, _ = record_listening_event(db, account_id=user.id, payload=payload)
        preference = db.execute(
            select(UserArtistPreference).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()

        assert item.track_duration_seconds is None
        assert item.completion_ratio == pytest.approx(0.75)
        assert item.completed is False
        assert item.skipped is False
        assert preference.behavior_weight == pytest.approx(
            RECOMMENDATION_CONFIG.substantial_play_weight,
            abs=0.01,
        )

    engine.dispose()


def test_client_completed_flag_cannot_reward_a_seek_to_end() -> None:
    engine = _engine()
    with Session(engine) as db:
        user, artist, track = _catalog(db, duration=180, suffix="seek-end")
        payload = _payload("seek-end-event-0001", track, artist, 2)
        payload.completed = True

        item, _ = record_listening_event(db, account_id=user.id, payload=payload)
        preference = db.execute(
            select(UserArtistPreference).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()

        assert item.completed is False
        assert item.skipped is True
        assert preference.behavior_weight == pytest.approx(
            RECOMMENDATION_CONFIG.quick_skip_weight,
            abs=0.01,
        )

    engine.dispose()


def test_repeated_detailed_play_receives_repeat_signal() -> None:
    engine = _engine()
    with Session(engine) as db:
        user, artist, track = _catalog(db, suffix="repeat")
        record_listening_event(
            db,
            account_id=user.id,
            payload=_payload("repeat-event-0001", track, artist, 95),
        )
        record_listening_event(
            db,
            account_id=user.id,
            payload=_payload("repeat-event-0002", track, artist, 95),
        )
        preference = db.execute(
            select(UserArtistPreference).where(
                UserArtistPreference.user_id == user.id,
                UserArtistPreference.artist_id == artist.id,
            )
        ).scalar_one()

        expected = RECOMMENDATION_CONFIG.completed_play_weight * 2 + RECOMMENDATION_CONFIG.repeat_play_weight
        assert preference.behavior_weight == pytest.approx(expected, abs=0.02)

    engine.dispose()


def test_event_id_cannot_be_reused_for_another_account_or_track() -> None:
    engine = _engine()
    with Session(engine) as db:
        first_user, first_artist, first_track = _catalog(db, suffix="collision-one")
        second_user, second_artist, second_track = _catalog(db, suffix="collision-two")
        event_id = "collision-event-0001"
        record_listening_event(
            db,
            account_id=first_user.id,
            payload=_payload(event_id, first_track, first_artist, 20),
        )

        with pytest.raises(ValueError):
            record_listening_event(
                db,
                account_id=second_user.id,
                payload=_payload(event_id, second_track, second_artist, 20),
            )
        with pytest.raises(ValueError):
            record_listening_event(
                db,
                account_id=first_user.id,
                payload=_payload(event_id, second_track, second_artist, 20),
            )

    engine.dispose()
