from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ListeningHistory(Base):
    __tablename__ = "listening_history"
    __table_args__ = (UniqueConstraint("user_id", "track_id", name="uq_listening_history_user_track"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="local", index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    track = relationship("Track")
