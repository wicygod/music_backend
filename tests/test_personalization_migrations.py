import os
from pathlib import Path

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_clean_database_upgrades_through_full_personalization_chain(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "personalization-chain.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("MUSIC_DATABASE_URL", raising=False)

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    engine = create_engine(database_url, future=True)
    inspector = inspect(engine)
    assert {"user_artist_preferences", "recommendation_events"} <= set(inspector.get_table_names())
    assert {
        "event_id",
        "artist_id",
        "listened_duration_seconds",
        "track_duration_seconds",
        "completion_ratio",
        "completed",
        "skipped",
        "context",
    } <= {column["name"] for column in inspector.get_columns("listening_history")}
    assert "music_preferences_completed_at" in {
        column["name"] for column in inspector.get_columns("users")
    }
    preference_foreign_keys = {
        tuple(item["constrained_columns"])
        for item in inspector.get_foreign_keys("user_artist_preferences")
    }
    history_foreign_keys = {
        tuple(item["constrained_columns"])
        for item in inspector.get_foreign_keys("listening_history")
    }
    assert {("user_id",), ("artist_id",)} <= preference_foreign_keys
    assert "explicit_source" in {
        column["name"] for column in inspector.get_columns("user_artist_preferences")
    }
    assert ("artist_id",) in history_foreign_keys
    history_indexes = {item["name"]: item for item in inspector.get_indexes("listening_history")}
    assert history_indexes["uq_listening_history_legacy_user_track"]["unique"] == 1
    artist_columns = {column["name"] for column in inspector.get_columns("artists")}
    assert {
        "is_canonical",
        "source_followers_count",
        "source_verified",
        "profile_resolved_at",
    } <= artist_columns
    artist_indexes = {item["name"] for item in inspector.get_indexes("artists")}
    assert {
        "ix_artists_is_canonical",
        "ix_artists_source_followers_count",
        "ix_artists_canonical_popularity",
    } <= artist_indexes
    assert {"albums", "album_tracks"} <= set(inspector.get_table_names())
    album_indexes = {item["name"] for item in inspector.get_indexes("albums")}
    assert {
        "ix_albums_artist_release_date",
        "ix_albums_normalized_title_popularity",
    } <= album_indexes
    album_track_indexes = {item["name"] for item in inspector.get_indexes("album_tracks")}
    assert "ix_album_tracks_album_order" in album_track_indexes
    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "0010_album_catalog"
    engine.dispose()


def test_album_catalog_downgrade_and_reupgrade_preserve_core_catalog(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "album-catalog-rollback.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("MUSIC_DATABASE_URL", raising=False)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0009_explicit_preference_source")
    engine = create_engine(database_url, future=True)
    tables_after_downgrade = set(inspect(engine).get_table_names())
    assert "albums" not in tables_after_downgrade
    assert "album_tracks" not in tables_after_downgrade
    assert {"artists", "tracks", "track_artists"} <= tables_after_downgrade
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url, future=True)
    assert {"albums", "album_tracks"} <= set(inspect(engine).get_table_names())
    engine.dispose()
