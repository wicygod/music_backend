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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserArtistPreference(Base):
    __tablename__ = "user_artist_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "artist_id", name="uq_user_artist_preference"),
        Index("ix_user_artist_preferences_user_weight", "user_id", "weight"),
        Index("ix_user_artist_preferences_artist_weight", "artist_id", "weight"),
        Index("ix_user_artist_preferences_user_explicit", "user_id", "explicit_selected"),
        Index("ix_user_artist_preferences_user_hidden", "user_id", "is_hidden"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="onboarding")
    explicit_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    behavior_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    explicit_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship("User", back_populates="artist_preferences")
    artist = relationship("Artist")


class RecommendationEvent(Base):
    __tablename__ = "recommendation_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_recommendation_events_event_id"),
        CheckConstraint("position IS NULL OR position >= 0", name="ck_recommendation_events_position"),
        Index("ix_recommendation_events_user_created", "user_id", "created_at"),
        Index("ix_recommendation_events_user_type_created", "user_id", "event_type", "created_at"),
        Index("ix_recommendation_events_track_created", "track_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    context: Mapped[str] = mapped_column(String(64), nullable=False, default="home")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="recommendation_events")
    track = relationship("Track")
