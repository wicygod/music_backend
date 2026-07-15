"""Harden legacy play aggregates for concurrent listening writes.

Revision ID: 0006_personalization_hardening
Revises: 0005_personalization
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_personalization_hardening"
down_revision = "0005_personalization"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_listening_history_legacy_user_track"


def _index_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(item["name"])
        for item in inspector.get_indexes("listening_history")
        if item.get("name")
    }


def _merge_legacy_duplicates() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT user_id, track_id, MIN(id) AS keeper_id, "
            "SUM(play_count) AS total_plays, MAX(played_at) AS latest_play "
            "FROM listening_history WHERE event_id IS NULL "
            "GROUP BY user_id, track_id HAVING COUNT(*) > 1"
        )
    ).mappings().all()
    for row in duplicates:
        connection.execute(
            sa.text(
                "UPDATE listening_history SET play_count = :total_plays, played_at = :latest_play "
                "WHERE id = :keeper_id"
            ),
            dict(row),
        )
        connection.execute(
            sa.text(
                "DELETE FROM listening_history WHERE user_id = :user_id AND track_id = :track_id "
                "AND event_id IS NULL AND id <> :keeper_id"
            ),
            dict(row),
        )


def upgrade() -> None:
    if INDEX_NAME in _index_names():
        return
    _merge_legacy_duplicates()
    op.create_index(
        INDEX_NAME,
        "listening_history",
        ["user_id", "track_id"],
        unique=True,
        sqlite_where=sa.text("event_id IS NULL"),
        postgresql_where=sa.text("event_id IS NULL"),
    )


def downgrade() -> None:
    if INDEX_NAME in _index_names():
        op.drop_index(INDEX_NAME, table_name="listening_history")
