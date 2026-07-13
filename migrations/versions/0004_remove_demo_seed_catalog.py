"""Remove the synthetic demo catalog.

Revision ID: 0004_remove_demo_seed_catalog
Revises: 0003_artist_import_indexes
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_remove_demo_seed_catalog"
down_revision = "0003_artist_import_indexes"
branch_labels = None
depends_on = None


DEMO_TRACK_FILTER = "source_name = 'demo_seed' OR cover_url LIKE '/static/covers/demo-%'"


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("listening_history", "user_favorites", "user_playlist_tracks", "track_artists"):
        connection.execute(
            sa.text(f"DELETE FROM {table} WHERE track_id IN (SELECT id FROM tracks WHERE {DEMO_TRACK_FILTER})")
        )
    connection.execute(sa.text(f"DELETE FROM tracks WHERE {DEMO_TRACK_FILTER}"))
    connection.execute(
        sa.text(
            "DELETE FROM artists "
            "WHERE source_name = 'demo_seed' "
            "AND NOT EXISTS (SELECT 1 FROM track_artists WHERE track_artists.artist_id = artists.id)"
        )
    )


def downgrade() -> None:
    # Synthetic records are intentionally not recreated.
    pass
