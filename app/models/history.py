from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ListeningHistory(Base):
    __tablename__ = "listening_history"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_listening_history_event_id"),
        CheckConstraint(
            "listened_duration_seconds >= 0",
            name="ck_listening_history_listened_duration",
        ),
        CheckConstraint(
            "track_duration_seconds IS NULL OR track_duration_seconds >= 0",
            name="ck_listening_history_track_duration",
        ),
        CheckConstraint(
            "completion_ratio IS NULL OR (completion_ratio >= 0 AND completion_ratio <= 1)",
            name="ck_listening_history_completion_ratio",
        ),
        Index("ix_listening_history_user_created", "user_id", "created_at"),
        Index("ix_listening_history_user_track_created", "user_id", "track_id", "created_at"),
        Index("ix_listening_history_user_artist_created", "user_id", "artist_id", "created_at"),
        Index(
            "uq_listening_history_legacy_user_track",
            "user_id",
            "track_id",
            unique=True,
            sqlite_where=text("event_id IS NULL"),
            postgresql_where=text("event_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="local", index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artist_id: Mapped[int | None] = mapped_column(
        ForeignKey("artists.id", ondelete="SET NULL"),
        nullable=True,
    )
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    listened_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    track_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    recommendation_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recommendation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    algorithm_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    track = relationship("Track")
    artist = relationship("Artist")
