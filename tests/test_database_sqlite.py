from sqlalchemy import text

from app.database import engine


def test_sqlite_enforces_foreign_keys_and_waits_for_short_locks() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() >= 5_000
