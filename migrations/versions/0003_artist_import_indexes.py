"""Add indexes used by artist import queues.

Revision ID: 0003_artist_import_indexes
Revises: 0002_listening_play_count
"""
from __future__ import annotations

from alembic import op


revision = "0003_artist_import_indexes"
down_revision = "0002_listening_play_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_artists_import_status", "artists", ["import_status"], unique=False)
    op.create_index("ix_artists_priority", "artists", ["priority"], unique=False)
    op.create_index("ix_artists_seed_source", "artists", ["seed_source"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_artists_seed_source", table_name="artists")
    op.drop_index("ix_artists_priority", table_name="artists")
    op.drop_index("ix_artists_import_status", table_name="artists")
