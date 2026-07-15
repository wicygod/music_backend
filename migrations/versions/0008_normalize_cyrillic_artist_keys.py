"""Normalize legacy Cyrillic yo variants in search keys.

Revision ID: 0008_normalize_cyrillic_artist_keys
Revises: 0007_canonical_artist_profiles
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_normalize_cyrillic_artist_keys"
down_revision = "0007_canonical_artist_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE artists SET normalized_name = "
            "replace(replace(normalized_name, 'ё', 'е'), 'Ё', 'е')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE tracks SET normalized_title = "
            "replace(replace(normalized_title, 'ё', 'е'), 'Ё', 'е')"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE search_cache SET query_normalized = "
            "replace(replace(query_normalized, 'ё', 'е'), 'Ё', 'е')"
        )
    )


def downgrade() -> None:
    # Normalization is intentionally irreversible: both spellings are valid
    # display text, while the search key must remain a single identity.
    pass
