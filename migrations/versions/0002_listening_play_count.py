"""Count repeated plays without duplicating recent-history rows.

Revision ID: 0002_listening_play_count
Revises: 0001_schema_baseline
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_listening_play_count"
down_revision = "0001_schema_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("listening_history")}
    if "play_count" not in columns:
        with op.batch_alter_table("listening_history") as batch_op:
            batch_op.add_column(sa.Column("play_count", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("listening_history")}
    if "play_count" in columns:
        with op.batch_alter_table("listening_history") as batch_op:
            batch_op.drop_column("play_count")
