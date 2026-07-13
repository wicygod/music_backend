import os

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.history import ListeningHistory
from app.models.track import Track
from app.routers.admin import _top_tracks


def test_admin_top_tracks_sums_repeat_plays_across_listeners() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        repeated = Track(title="Repeated", normalized_title="repeated", is_playable=True)
        single = Track(title="Single", normalized_title="single", is_playable=True)
        db.add_all([repeated, single])
        db.flush()
        db.add_all(
            [
                ListeningHistory(user_id="account:1", track_id=repeated.id, play_count=4),
                ListeningHistory(user_id="account:2", track_id=repeated.id, play_count=3),
                ListeningHistory(user_id="account:1", track_id=single.id, play_count=2),
            ]
        )
        db.commit()

        top = _top_tracks(db, limit=10)

        assert [(item["track"]["title"], item["play_count"]) for item in top] == [
            ("Repeated", 7),
            ("Single", 2),
        ]

    engine.dispose()
