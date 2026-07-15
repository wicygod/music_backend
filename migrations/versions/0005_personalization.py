"""Add artist preferences, detailed listening events and recommendation analytics.

Revision ID: 0005_personalization
Revises: 0004_remove_demo_seed_catalog
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_personalization"
down_revision = "0004_remove_demo_seed_catalog"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    # DDL invalidates inspector caches, so every guard uses a fresh instance.
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    return _inspector().has_table(table)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {str(column["name"]) for column in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {str(index["name"]) for index in _inspector().get_indexes(table) if index.get("name")}


def _unique_constraints(table: str) -> list[dict]:
    if not _table_exists(table):
        return []
    return list(_inspector().get_unique_constraints(table))


def _foreign_keys(table: str) -> list[dict]:
    if not _table_exists(table):
        return []
    return list(_inspector().get_foreign_keys(table))


def _check_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {
        str(constraint["name"])
        for constraint in _inspector().get_check_constraints(table)
        if constraint.get("name")
    }


def _constraint_for_columns(constraints: list[dict], columns: list[str]) -> dict | None:
    expected = tuple(columns)
    return next(
        (
            constraint
            for constraint in constraints
            if tuple(str(item) for item in constraint.get("column_names") or ()) == expected
        ),
        None,
    )


def _foreign_key_for_columns(constraints: list[dict], columns: list[str]) -> dict | None:
    expected = tuple(columns)
    return next(
        (
            constraint
            for constraint in constraints
            if tuple(str(item) for item in constraint.get("constrained_columns") or ()) == expected
        ),
        None,
    )


def _ensure_index(table: str, name: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns, unique=False)


def _create_preferences_table() -> None:
    op.create_table(
        "user_artist_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("artist_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="onboarding"),
        sa.Column("explicit_weight", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("behavior_weight", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("weight", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("explicit_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["artist_id"], ["artists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "artist_id", name="uq_user_artist_preference"),
    )


def _ensure_preferences_indexes() -> None:
    _ensure_index(
        "user_artist_preferences",
        "ix_user_artist_preferences_user_weight",
        ["user_id", "weight"],
    )
    _ensure_index(
        "user_artist_preferences",
        "ix_user_artist_preferences_artist_weight",
        ["artist_id", "weight"],
    )
    _ensure_index(
        "user_artist_preferences",
        "ix_user_artist_preferences_user_explicit",
        ["user_id", "explicit_selected"],
    )
    _ensure_index(
        "user_artist_preferences",
        "ix_user_artist_preferences_user_hidden",
        ["user_id", "is_hidden"],
    )


def _create_recommendation_events_table() -> None:
    op.create_table(
        "recommendation_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("recommendation_type", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("algorithm_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column("context", sa.String(length=64), nullable=False, server_default="home"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("position IS NULL OR position >= 0", name="ck_recommendation_events_position"),
        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_recommendation_events_event_id"),
    )


def _ensure_recommendation_indexes() -> None:
    _ensure_index(
        "recommendation_events",
        "ix_recommendation_events_user_created",
        ["user_id", "created_at"],
    )
    _ensure_index(
        "recommendation_events",
        "ix_recommendation_events_user_type_created",
        ["user_id", "event_type", "created_at"],
    )
    _ensure_index(
        "recommendation_events",
        "ix_recommendation_events_track_created",
        ["track_id", "created_at"],
    )


def _ensure_history_columns_and_constraints() -> None:
    columns = _column_names("listening_history")
    uniques = _unique_constraints("listening_history")
    foreign_keys = _foreign_keys("listening_history")
    checks = _check_names("listening_history")
    legacy_unique = _constraint_for_columns(uniques, ["user_id", "track_id"])
    event_unique = _constraint_for_columns(uniques, ["event_id"])
    artist_foreign_key = _foreign_key_for_columns(foreign_keys, ["artist_id"])

    new_columns: list[sa.Column] = [
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("artist_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("listened_duration_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("track_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("completion_ratio", sa.Float(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("context", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("recommendation_type", sa.String(length=64), nullable=True),
        sa.Column("recommendation_reason", sa.String(length=255), nullable=True),
        sa.Column("algorithm_version", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    ]
    missing_columns = [column for column in new_columns if column.name not in columns]
    needs_batch = bool(
        missing_columns
        or legacy_unique
        or not event_unique
        or not artist_foreign_key
        or "ck_listening_history_listened_duration" not in checks
        or "ck_listening_history_track_duration" not in checks
        or "ck_listening_history_completion_ratio" not in checks
    )
    if not needs_batch:
        return

    with op.batch_alter_table("listening_history") as batch_op:
        if legacy_unique and legacy_unique.get("name"):
            batch_op.drop_constraint(str(legacy_unique["name"]), type_="unique")
        for column in missing_columns:
            batch_op.add_column(column)
        if not artist_foreign_key:
            batch_op.create_foreign_key(
                "fk_listening_history_artist_id",
                "artists",
                ["artist_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if not event_unique:
            batch_op.create_unique_constraint("uq_listening_history_event_id", ["event_id"])
        if "ck_listening_history_listened_duration" not in checks:
            batch_op.create_check_constraint(
                "ck_listening_history_listened_duration",
                "listened_duration_seconds >= 0",
            )
        if "ck_listening_history_track_duration" not in checks:
            batch_op.create_check_constraint(
                "ck_listening_history_track_duration",
                "track_duration_seconds IS NULL OR track_duration_seconds >= 0",
            )
        if "ck_listening_history_completion_ratio" not in checks:
            batch_op.create_check_constraint(
                "ck_listening_history_completion_ratio",
                "completion_ratio IS NULL OR (completion_ratio >= 0 AND completion_ratio <= 1)",
            )


def _ensure_history_indexes() -> None:
    _ensure_index(
        "listening_history",
        "ix_listening_history_user_created",
        ["user_id", "created_at"],
    )
    _ensure_index(
        "listening_history",
        "ix_listening_history_user_track_created",
        ["user_id", "track_id", "created_at"],
    )
    _ensure_index(
        "listening_history",
        "ix_listening_history_user_artist_created",
        ["user_id", "artist_id", "created_at"],
    )


def upgrade() -> None:
    if "music_preferences_completed_at" not in _column_names("users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("music_preferences_completed_at", sa.DateTime(), nullable=True))

    # Existing accounts keep their current home experience instead of being
    # forced through an onboarding step after the deployment.
    op.execute(
        sa.text(
            "UPDATE users "
            "SET music_preferences_completed_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
            "WHERE music_preferences_completed_at IS NULL"
        )
    )

    if not _table_exists("user_artist_preferences"):
        _create_preferences_table()
    _ensure_preferences_indexes()

    _ensure_history_columns_and_constraints()
    op.execute(
        sa.text(
            "UPDATE listening_history "
            "SET created_at = COALESCE(created_at, played_at, CURRENT_TIMESTAMP)"
        )
    )
    _ensure_history_indexes()

    if not _table_exists("recommendation_events"):
        _create_recommendation_events_table()
    _ensure_recommendation_indexes()


def _drop_index_if_present(table: str, name: str) -> None:
    if name in _index_names(table):
        op.drop_index(name, table_name=table)


def downgrade() -> None:
    if _table_exists("recommendation_events"):
        _drop_index_if_present("recommendation_events", "ix_recommendation_events_track_created")
        _drop_index_if_present("recommendation_events", "ix_recommendation_events_user_type_created")
        _drop_index_if_present("recommendation_events", "ix_recommendation_events_user_created")
        op.drop_table("recommendation_events")

    history_columns = _column_names("listening_history")
    if "event_id" in history_columns:
        # Only detailed events are incompatible with the restored aggregate
        # uniqueness rule.
        op.execute(sa.text("DELETE FROM listening_history WHERE event_id IS NOT NULL"))
    _drop_index_if_present("listening_history", "ix_listening_history_user_artist_created")
    _drop_index_if_present("listening_history", "ix_listening_history_user_track_created")
    _drop_index_if_present("listening_history", "ix_listening_history_user_created")

    if history_columns.intersection(
        {
            "event_id",
            "artist_id",
            "started_at",
            "listened_duration_seconds",
            "track_duration_seconds",
            "completion_ratio",
            "completed",
            "skipped",
            "context",
            "recommendation_type",
            "recommendation_reason",
            "algorithm_version",
            "created_at",
        }
    ):
        checks = _check_names("listening_history")
        uniques = _unique_constraints("listening_history")
        foreign_keys = _foreign_keys("listening_history")
        event_unique = _constraint_for_columns(uniques, ["event_id"])
        legacy_unique = _constraint_for_columns(uniques, ["user_id", "track_id"])
        artist_foreign_key = _foreign_key_for_columns(foreign_keys, ["artist_id"])
        with op.batch_alter_table("listening_history") as batch_op:
            for name in (
                "ck_listening_history_completion_ratio",
                "ck_listening_history_track_duration",
                "ck_listening_history_listened_duration",
            ):
                if name in checks:
                    batch_op.drop_constraint(name, type_="check")
            if event_unique and event_unique.get("name"):
                batch_op.drop_constraint(str(event_unique["name"]), type_="unique")
            if artist_foreign_key and artist_foreign_key.get("name"):
                batch_op.drop_constraint(str(artist_foreign_key["name"]), type_="foreignkey")
            for name in (
                "created_at",
                "algorithm_version",
                "recommendation_reason",
                "recommendation_type",
                "context",
                "skipped",
                "completed",
                "completion_ratio",
                "track_duration_seconds",
                "listened_duration_seconds",
                "started_at",
                "artist_id",
                "event_id",
            ):
                if name in history_columns:
                    batch_op.drop_column(name)
            if not legacy_unique:
                batch_op.create_unique_constraint(
                    "uq_listening_history_user_track",
                    ["user_id", "track_id"],
                )

    if _table_exists("user_artist_preferences"):
        _drop_index_if_present("user_artist_preferences", "ix_user_artist_preferences_user_hidden")
        _drop_index_if_present("user_artist_preferences", "ix_user_artist_preferences_user_explicit")
        _drop_index_if_present("user_artist_preferences", "ix_user_artist_preferences_artist_weight")
        _drop_index_if_present("user_artist_preferences", "ix_user_artist_preferences_user_weight")
        op.drop_table("user_artist_preferences")

    if "music_preferences_completed_at" in _column_names("users"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("music_preferences_completed_at")
