"""Add a normalized album catalog and ordered track membership.

Revision ID: 0010_album_catalog
Revises: 0009_explicit_preference_source
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_album_catalog"
down_revision = "0009_explicit_preference_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "albums" not in tables:
        op.create_table(
            "albums",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("artist_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("normalized_title", sa.String(length=512), nullable=False),
            sa.Column("album_type", sa.String(length=32), nullable=False, server_default="album"),
            sa.Column("cover_url", sa.String(length=1024), nullable=True),
            sa.Column("release_date", sa.DateTime(), nullable=True),
            sa.Column("track_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_name", sa.String(length=128), nullable=False, server_default="soundcloud"),
            sa.Column("source_external_id", sa.String(length=255), nullable=False),
            sa.Column("source_url", sa.String(length=1024), nullable=False),
            sa.Column("popularity_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["artist_id"], ["artists.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_name", "source_external_id", name="uq_album_source_external_id"),
        )
        op.create_index("ix_albums_artist_id", "albums", ["artist_id"])
        op.create_index("ix_albums_normalized_title", "albums", ["normalized_title"])
        op.create_index("ix_albums_album_type", "albums", ["album_type"])
        op.create_index("ix_albums_release_date", "albums", ["release_date"])
        op.create_index("ix_albums_source_name", "albums", ["source_name"])
        op.create_index("ix_albums_popularity_score", "albums", ["popularity_score"])
        op.create_index("ix_albums_is_available", "albums", ["is_available"])
        op.create_index("ix_albums_artist_release_date", "albums", ["artist_id", "release_date"])
        op.create_index(
            "ix_albums_normalized_title_popularity",
            "albums",
            ["normalized_title", "popularity_score"],
        )

    inspector = sa.inspect(op.get_bind())
    if "album_tracks" not in set(inspector.get_table_names()):
        op.create_table(
            "album_tracks",
            sa.Column("album_id", sa.Integer(), nullable=False),
            sa.Column("track_id", sa.Integer(), nullable=False),
            sa.Column("disc_number", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("track_number", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["album_id"], ["albums.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("album_id", "track_id"),
            sa.UniqueConstraint("album_id", "disc_number", "track_number", name="uq_album_disc_track_number"),
        )
        op.create_index("ix_album_tracks_track_id", "album_tracks", ["track_id"])
        op.create_index("ix_album_tracks_album_order", "album_tracks", ["album_id", "disc_number", "track_number"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "album_tracks" in tables:
        op.drop_table("album_tracks")
    if "albums" in tables:
        op.drop_table("albums")
