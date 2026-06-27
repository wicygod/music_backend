from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserPlaylist(Base):
    __tablename__ = "user_playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="local-user", index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    track_links = relationship("UserPlaylistTrack", back_populates="playlist", cascade="all, delete-orphan")


class UserPlaylistTrack(Base):
    __tablename__ = "user_playlist_tracks"

    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("user_playlists.id", ondelete="CASCADE"),
        primary_key=True,
    )
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    playlist = relationship("UserPlaylist", back_populates="track_links")
    track = relationship("Track")


class UserFavorite(Base):
    __tablename__ = "user_favorites"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True, default="local-user")
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    track = relationship("Track")
