from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    normalized_title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    language: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    popularity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_playable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    audio_src: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    artist_links = relationship("TrackArtist", back_populates="track", cascade="all, delete-orphan")
    album_links = relationship("AlbumTrack", back_populates="track", cascade="all, delete-orphan")


class TrackArtist(Base):
    __tablename__ = "track_artists"
    __table_args__ = (UniqueConstraint("track_id", "artist_id", "role", name="uq_track_artist_role"),)

    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True, default="main")

    track = relationship("Track", back_populates="artist_links")
    artist = relationship("Artist", back_populates="track_links")


class SearchCache(Base):
    __tablename__ = "search_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    query_normalized: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(128), nullable=False, default="local")
    response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
