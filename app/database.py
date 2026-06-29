import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DATABASE_URL = os.getenv("MUSIC_DATABASE_URL", "sqlite:///./music_catalog.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_dev_schema()


def migrate_dev_schema() -> None:
    """Small dev-only schema drift helper until Alembic is introduced."""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "artists" not in inspector.get_table_names():
        return

    table_names = set(inspector.get_table_names())
    columns = {column["name"] for column in inspector.get_columns("artists")}
    artist_columns = {
        "priority": "VARCHAR(32) NOT NULL DEFAULT 'normal'",
        "tracks_target": "INTEGER NOT NULL DEFAULT 25",
        "seed_source": "VARCHAR(128)",
        "import_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
        "last_imported_at": "DATETIME",
    }

    with engine.begin() as connection:
        for name, definition in artist_columns.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE artists ADD COLUMN {name} {definition}"))

        if "listening_history" in table_names:
            history_columns = {column["name"] for column in inspector.get_columns("listening_history")}
            if "user_id" not in history_columns:
                connection.execute(
                    text("ALTER TABLE listening_history ADD COLUMN user_id VARCHAR(128) NOT NULL DEFAULT 'local'")
                )
