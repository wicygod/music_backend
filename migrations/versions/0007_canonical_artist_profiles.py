"""Add canonical provider-profile metadata for artists.

Revision ID: 0007_canonical_artist_profiles
Revises: 0006_personalization_hardening
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_canonical_artist_profiles"
down_revision = "0006_personalization_hardening"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns("artists")}


def _index_names() -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes("artists")
        if item.get("name")
    }


def upgrade() -> None:
    columns = _column_names()
    with op.batch_alter_table("artists") as batch:
        if "source_followers_count" not in columns:
            batch.add_column(
                sa.Column("source_followers_count", sa.Integer(), nullable=False, server_default="0")
            )
        if "source_verified" not in columns:
            batch.add_column(
                sa.Column("source_verified", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "is_canonical" not in columns:
            batch.add_column(
                sa.Column("is_canonical", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "profile_resolved_at" not in columns:
            batch.add_column(sa.Column("profile_resolved_at", sa.DateTime(), nullable=True))

    indexes = _index_names()
    if "ix_artists_source_followers_count" not in indexes:
        op.create_index(
            "ix_artists_source_followers_count",
            "artists",
            ["source_followers_count"],
            unique=False,
        )
    if "ix_artists_is_canonical" not in indexes:
        op.create_index("ix_artists_is_canonical", "artists", ["is_canonical"], unique=False)
    if "ix_artists_canonical_popularity" not in indexes:
        op.create_index(
            "ix_artists_canonical_popularity",
            "artists",
            ["is_canonical", "source_verified", "source_followers_count"],
            unique=False,
        )


def downgrade() -> None:
    indexes = _index_names()
    for name in (
        "ix_artists_canonical_popularity",
        "ix_artists_is_canonical",
        "ix_artists_source_followers_count",
    ):
        if name in indexes:
            op.drop_index(name, table_name="artists")

    columns = _column_names()
    with op.batch_alter_table("artists") as batch:
        for name in (
            "profile_resolved_at",
            "is_canonical",
            "source_verified",
            "source_followers_count",
        ):
            if name in columns:
                batch.drop_column(name)
