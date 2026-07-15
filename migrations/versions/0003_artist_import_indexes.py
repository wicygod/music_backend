"""Add indexes used by artist import queues.

Revision ID: 0003_artist_import_indexes
Revises: 0002_listening_play_count
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_artist_import_indexes"
down_revision = "0002_listening_play_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("artists")}
    for name, columns in (
        ("ix_artists_import_status", ["import_status"]),
        ("ix_artists_priority", ["priority"]),
        ("ix_artists_seed_source", ["seed_source"]),
    ):
        if name not in existing:
            op.create_index(name, "artists", columns, unique=False)


def downgrade() -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("artists")}
    for name in (
        "ix_artists_seed_source",
        "ix_artists_priority",
        "ix_artists_import_status",
    ):
        if name in existing:
            op.drop_index(name, table_name="artists")
