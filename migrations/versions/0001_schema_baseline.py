"""Create the catalog schema and adopt legacy SQLite databases.

Revision ID: 0001_schema_baseline
Revises:
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app import models  # noqa: F401
from app.database import Base


revision = "0001_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _add_missing_columns(table: str, columns: dict[str, sa.Column]) -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(bind=connection)

    _add_missing_columns(
        "artists",
        {
            "priority": sa.Column("priority", sa.String(32), nullable=False, server_default="normal"),
            "tracks_target": sa.Column("tracks_target", sa.Integer(), nullable=False, server_default="25"),
            "seed_source": sa.Column("seed_source", sa.String(128), nullable=True),
            "import_status": sa.Column("import_status", sa.String(32), nullable=False, server_default="pending"),
            "last_imported_at": sa.Column("last_imported_at", sa.DateTime(), nullable=True),
        },
    )
    _add_missing_columns(
        "listening_history",
        {
            "user_id": sa.Column("user_id", sa.String(128), nullable=False, server_default="local"),
        },
    )
    _add_missing_columns(
        "users",
        {
            "login": sa.Column("login", sa.String(64), nullable=True),
            "nickname": sa.Column("nickname", sa.String(96), nullable=False, server_default="User"),
            "password_hash": sa.Column("password_hash", sa.String(256), nullable=True),
            "avatar_url": sa.Column("avatar_url", sa.Text(), nullable=True),
            "subscription_status": sa.Column(
                "subscription_status", sa.String(32), nullable=False, server_default="inactive"
            ),
            "total_listening_seconds": sa.Column(
                "total_listening_seconds", sa.Integer(), nullable=False, server_default="0"
            ),
            "created_at": sa.Column("created_at", sa.DateTime(), nullable=True),
        },
    )


def downgrade() -> None:
    # The baseline may adopt an existing catalog, so dropping it is intentionally unsafe.
    pass
