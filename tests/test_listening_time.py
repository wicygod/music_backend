from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.user import User
from app.repositories.history import add_listening_time, get_history_summary


def test_listening_time_starts_at_zero_and_counts_repeated_sessions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(login="listener", nickname="Listener", password_hash="hash")
        db.add(user)
        db.commit()
        db.refresh(user)

        initial = get_history_summary(db, user_id=f"account:{user.id}", account_id=user.id)
        add_listening_time(db, account_id=user.id, seconds=180)
        add_listening_time(db, account_id=user.id, seconds=180)
        repeated = get_history_summary(db, user_id=f"account:{user.id}", account_id=user.id)

    engine.dispose()
    assert initial["total_seconds"] == 0
    assert repeated["total_seconds"] == 360
