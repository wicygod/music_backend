from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (
        UniqueConstraint("source_name", "source_external_id", name="uq_album_source_external_id"),
        Index("ix_albums_artist_release_date", "artist_id", "release_date"),
        Index("ix_albums_normalized_title_popularity", "normalized_title", "popularity_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    album_type: Mapped[str] = mapped_column(String(32), nullable=False, default="album", index=True)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    release_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    track_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False, default="soundcloud", index=True)
    source_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    popularity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    artist = relationship("Artist", back_populates="albums")
    track_links = relationship(
        "AlbumTrack",
        back_populates="album",
        cascade="all, delete-orphan",
        order_by=lambda: (AlbumTrack.disc_number, AlbumTrack.track_number, AlbumTrack.track_id),
    )


class AlbumTrack(Base):
    __tablename__ = "album_tracks"
    __table_args__ = (
        UniqueConstraint("album_id", "disc_number", "track_number", name="uq_album_disc_track_number"),
        Index("ix_album_tracks_album_order", "album_id", "disc_number", "track_number"),
    )

    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True, index=True)
    disc_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    track_number: Mapped[int] = mapped_column(Integer, nullable=False)

    album = relationship("Album", back_populates="track_links")
    track = relationship("Track", back_populates="album_links")
