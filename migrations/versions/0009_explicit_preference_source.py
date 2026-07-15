"""Preserve the source of an explicit artist choice.

Revision ID: 0009_explicit_preference_source
Revises: 0008_normalize_cyrillic_artist_keys
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_explicit_preference_source"
down_revision = "0008_normalize_cyrillic_artist_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_artist_preferences")
    }
    if "explicit_source" not in columns:
        with op.batch_alter_table("user_artist_preferences") as batch_op:
            batch_op.add_column(sa.Column("explicit_source", sa.String(length=32), nullable=True))

    # Existing explicit choices were created by the initial onboarding unless
    # their latest explicit edit is still identifiable as a Settings action.
    op.execute(
        sa.text(
            """
            UPDATE user_artist_preferences
            SET explicit_source = CASE
                WHEN source = 'settings' THEN 'settings'
                ELSE 'onboarding'
            END
            WHERE explicit_selected = 1 AND explicit_source IS NULL
            """
        )
    )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_artist_preferences")
    }
    if "explicit_source" in columns:
        with op.batch_alter_table("user_artist_preferences") as batch_op:
            batch_op.drop_column("explicit_source")
