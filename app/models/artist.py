from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    genres_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    popularity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    source_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_followers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    source_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    profile_resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="normal", index=True)
    tracks_target: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    seed_source: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    import_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    track_links = relationship("TrackArtist", back_populates="artist", cascade="all, delete-orphan")
    albums = relationship("Album", back_populates="artist", cascade="all, delete-orphan")
