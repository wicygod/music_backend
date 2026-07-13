from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.history import ListeningHistory
from app.models.track import Track
from app.repositories.history import record_track_play


def test_repeated_play_increments_counter_and_keeps_one_recent_row() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        track = Track(title="Again", normalized_title="again", is_playable=True)
        db.add(track)
        db.commit()
        db.refresh(track)

        record_track_play(db, track.id, user_id="account:1")
        record_track_play(db, track.id, user_id="account:1")
        history = db.execute(select(ListeningHistory)).scalar_one()

        assert history.play_count == 2

    engine.dispose()
