from datetime import datetime, timedelta, timezone
from hashlib import blake2b

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.history import ListeningHistory
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.models.personalization import RecommendationEvent
from app.repositories.tracks import get_track
from app.repositories.personalization import apply_preference_signal, primary_artist_id_for_track
from app.schemas.personalization import ListeningEventCreate
from app.services.recommendation_config import RECOMMENDATION_CONFIG


DEFAULT_USER_ID = "local"


def record_listening_event(
    db: Session,
    *,
    account_id: int,
    payload: ListeningEventCreate,
) -> tuple[ListeningHistory, bool]:
    """Persist one immutable playback cycle in the existing history table."""

    existing = _listening_event_by_id(db, payload.event_id)
    if existing is not None:
        _assert_same_listening_event(existing, account_id=account_id, track_id=payload.track_id)
        return existing, False

    track = get_track(db, payload.track_id)
    if not track or not track.is_playable or track.needs_review:
        raise LookupError("Track not found or unavailable")
    linked_artist_ids = {int(link.artist_id) for link in track.artist_links}
    artist_id = int(payload.artist_id) if payload.artist_id in linked_artist_ids else None
    artist_id = artist_id or primary_artist_id_for_track(db, track.id)

    track_duration = int(payload.track_duration_seconds or track.duration_seconds or 0) or None
    listened_duration = max(0, int(payload.listened_duration_seconds or 0))
    if track_duration:
        computed_ratio = min(1.0, listened_duration / max(1, track_duration))
    else:
        computed_ratio = min(1.0, max(0.0, float(payload.completion_ratio or 0.0)))
    if track_duration:
        # Never trust a client-side `ended` flag for known-duration tracks:
        # seeking to the final seconds also fires ended in WebView/audio APIs.
        completed = computed_ratio >= RECOMMENDATION_CONFIG.completed_ratio
    else:
        completed = bool(
            payload.completed
            and listened_duration >= max(RECOMMENDATION_CONFIG.quick_skip_seconds, 30)
        )
    quick_skip = listened_duration < RECOMMENDATION_CONFIG.quick_skip_seconds and not completed
    skipped = bool(payload.skipped or quick_skip)
    now = datetime.utcnow()
    started_at = _naive_utc(payload.started_at)

    item = ListeningHistory(
        user_id=f"account:{int(account_id)}",
        track_id=track.id,
        event_id=payload.event_id,
        artist_id=artist_id,
        play_count=1,
        played_at=now,
        started_at=started_at,
        listened_duration_seconds=listened_duration,
        track_duration_seconds=track_duration,
        completion_ratio=computed_ratio,
        completed=completed,
        skipped=skipped,
        context=payload.context,
        recommendation_type=payload.recommendation_type,
        recommendation_reason=payload.recommendation_reason,
        algorithm_version=payload.algorithm_version,
        created_at=now,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _listening_event_by_id(db, payload.event_id)
        if existing is None:
            raise
        _assert_same_listening_event(existing, account_id=account_id, track_id=payload.track_id)
        return existing, False

    if artist_id:
        signal_delta = _listening_signal_delta(
            db,
            account_id=account_id,
            track_id=track.id,
            artist_id=artist_id,
            completion_ratio=computed_ratio,
            completed=completed,
            skipped=skipped,
            listened_duration=listened_duration,
        )
        if signal_delta:
            apply_preference_signal(
                db,
                user_id=account_id,
                artist_id=artist_id,
                source="listening",
                delta=signal_delta,
                occurred_at=started_at,
                now=now,
            )

    if payload.recommendation_type:
        db.add(
            RecommendationEvent(
                event_id=f"playback:{blake2b(payload.event_id.encode('utf-8'), digest_size=16).hexdigest()}",
                user_id=account_id,
                track_id=track.id,
                event_type="recommendation_skipped" if skipped else "recommendation_played",
                recommendation_type=payload.recommendation_type,
                reason=payload.recommendation_reason,
                algorithm_version=payload.algorithm_version or RECOMMENDATION_CONFIG.algorithm_version,
                context=payload.context,
                created_at=now,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _listening_event_by_id(db, payload.event_id)
        if existing is None:
            raise
        _assert_same_listening_event(existing, account_id=account_id, track_id=payload.track_id)
        return existing, False
    db.refresh(item)
    from app.services.recommendation_service import invalidate_recommendations

    invalidate_recommendations(account_id)
    return item, True


def _listening_signal_delta(
    db: Session,
    *,
    account_id: int,
    track_id: int,
    artist_id: int,
    completion_ratio: float,
    completed: bool,
    skipped: bool,
    listened_duration: int,
) -> float:
    delta = 0.0
    if completed:
        delta += RECOMMENDATION_CONFIG.completed_play_weight
    elif completion_ratio >= RECOMMENDATION_CONFIG.substantial_ratio:
        delta += RECOMMENDATION_CONFIG.substantial_play_weight

    aggregate_play_count = db.execute(
        select(ListeningHistory.play_count).where(
            ListeningHistory.user_id == f"account:{int(account_id)}",
            ListeningHistory.track_id == track_id,
            ListeningHistory.event_id.is_(None),
        )
    ).scalar_one_or_none() or 0
    detailed_play_count = db.execute(
        select(func.count(ListeningHistory.id)).where(
            ListeningHistory.user_id == f"account:{int(account_id)}",
            ListeningHistory.track_id == track_id,
            ListeningHistory.event_id.is_not(None),
        )
    ).scalar_one()
    if int(aggregate_play_count) >= 2 or int(detailed_play_count or 0) >= 2:
        delta += RECOMMENDATION_CONFIG.repeat_play_weight

    if skipped and listened_duration < RECOMMENDATION_CONFIG.quick_skip_seconds:
        delta += RECOMMENDATION_CONFIG.quick_skip_weight
        recent_skip_count = db.execute(
            select(func.count(ListeningHistory.id)).where(
                ListeningHistory.user_id == f"account:{int(account_id)}",
                ListeningHistory.artist_id == artist_id,
                ListeningHistory.event_id.is_not(None),
                ListeningHistory.skipped.is_(True),
                ListeningHistory.created_at >= datetime.utcnow() - timedelta(days=14),
            )
        ).scalar_one()
        if int(recent_skip_count or 0) >= 3:
            delta += RECOMMENDATION_CONFIG.repeated_quick_skip_weight
    return delta


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _listening_event_by_id(db: Session, event_id: str) -> ListeningHistory | None:
    return db.execute(
        select(ListeningHistory).where(ListeningHistory.event_id == str(event_id))
    ).scalar_one_or_none()


def _assert_same_listening_event(
    event: ListeningHistory,
    *,
    account_id: int,
    track_id: int,
) -> None:
    if (
        event.user_id != f"account:{int(account_id)}"
        or int(event.track_id) != int(track_id)
    ):
        raise ValueError("event_id already belongs to another playback event")


def record_track_play(db: Session, track_id: int, user_id: str = DEFAULT_USER_ID) -> Track | None:
    track = get_track(db, track_id)
    if not track:
        return None

    now = datetime.utcnow()
    legacy_scope = (
        ListeningHistory.user_id == user_id,
        ListeningHistory.track_id == track_id,
        ListeningHistory.event_id.is_(None),
    )
    updated = db.execute(
        update(ListeningHistory)
        .where(*legacy_scope)
        .values(
            played_at=now,
            play_count=ListeningHistory.play_count + 1,
        )
    )
    if not updated.rowcount:
        db.add(ListeningHistory(user_id=user_id, track_id=track_id, play_count=1, played_at=now))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent first play may have inserted the aggregate row after
        # our UPDATE but before our INSERT. Retry as one atomic increment.
        db.rollback()
        db.execute(
            update(ListeningHistory)
            .where(*legacy_scope)
            .values(
                played_at=now,
                play_count=ListeningHistory.play_count + 1,
            )
        )
        db.commit()
    return get_track(db, track_id)


def list_recent_history_tracks(db: Session, limit: int = 36, user_id: str = DEFAULT_USER_ID) -> list[Track]:
    stmt = (
        select(Track)
        .join(ListeningHistory, ListeningHistory.track_id == Track.id)
        .where(
            ListeningHistory.user_id == user_id,
            ListeningHistory.event_id.is_(None),
        )
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(ListeningHistory.played_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().unique().all())


def add_listening_time(db: Session, account_id: int, seconds: int) -> None:
    safe_seconds = max(0, min(int(seconds), 300))
    if not safe_seconds:
        return
    db.execute(
        update(User)
        .where(User.id == account_id)
        .values(total_listening_seconds=User.total_listening_seconds + safe_seconds)
    )
    db.commit()


def get_history_summary(db: Session, user_id: str = DEFAULT_USER_ID, account_id: int | None = None) -> dict[str, int]:
    total_tracks = db.execute(
        select(func.count(ListeningHistory.id))
        .select_from(ListeningHistory)
        .where(ListeningHistory.user_id == user_id)
        .where(ListeningHistory.event_id.is_(None))
    ).scalar_one()
    total_seconds = 0
    if account_id is not None:
        total_seconds = db.execute(
            select(User.total_listening_seconds).where(User.id == account_id)
        ).scalar_one_or_none() or 0
    return {
        "total_seconds": max(0, int(total_seconds or 0)),
        "total_tracks": max(0, int(total_tracks or 0)),
    }
