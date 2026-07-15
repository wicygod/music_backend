from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.artist import Artist
from app.models.personalization import RecommendationEvent, UserArtistPreference
from app.models.track import Track
from app.models.user import User
from app.repositories.personalization import (
    ExplicitPreferenceDiff,
    apply_preference_signal,
    list_user_artist_preferences,
    primary_artist_id_for_track,
    replace_explicit_preferences,
    selected_artist_ids,
)
from app.services.recommendation_config import RECOMMENDATION_CONFIG, SIGNAL_WEIGHTS


_ALLOWED_PREFERENCE_SOURCES = {"onboarding", "settings"}
_EVENT_SIGNAL_TYPES = {
    "recommendation_liked": "like",
    "recommendation_skipped": "quick_skip",
}


class PreferenceServiceError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class MusicPreferenceSaveResult:
    completed_at: datetime
    selected_artist_ids: list[int]
    added_artist_ids: list[int]
    removed_artist_ids: list[int]
    skipped: bool
    preferences: list[UserArtistPreference]


@dataclass(frozen=True)
class MusicSignalResult:
    event: RecommendationEvent
    preference: UserArtistPreference | None
    created: bool


def save_music_preferences(
    db: Session,
    *,
    user_id: int,
    artist_ids: list[int] | tuple[int, ...],
    source: str = "onboarding",
    skipped: bool = False,
    now: datetime | None = None,
) -> MusicPreferenceSaveResult:
    """Atomically replace explicit choices and complete initial onboarding.

    The configured minimum applies only while onboarding is incomplete. Later
    settings edits may intentionally leave fewer (or zero) explicit artists.
    Repeating the same request assigns fixed explicit weights rather than
    incrementing them, making the endpoint idempotent.
    """

    timestamp = _naive_utc(now or datetime.utcnow())
    normalized_source = str(source or "").strip().lower()
    clean_ids = _deduplicate_positive_ids(artist_ids)

    try:
        if len(clean_ids) > 200:
            raise PreferenceServiceError(
                "At most 200 artists can be saved at once",
                code="too_many_artists",
            )
        if normalized_source not in _ALLOWED_PREFERENCE_SOURCES:
            raise PreferenceServiceError(
                "Unsupported music preference source",
                code="invalid_source",
            )
        if skipped and clean_ids:
            raise PreferenceServiceError(
                "Skipped onboarding cannot include artist IDs",
                code="skipped_with_artists",
            )

        user = db.execute(
            select(User).where(User.id == int(user_id)).with_for_update()
        ).scalar_one_or_none()
        if user is None:
            raise PreferenceServiceError(
                "User not found",
                code="user_not_found",
                status_code=404,
            )

        initial_onboarding = user.music_preferences_completed_at is None
        if initial_onboarding and not skipped:
            minimum = max(0, int(RECOMMENDATION_CONFIG.minimum_onboarding_artists))
            if len(clean_ids) < minimum:
                raise PreferenceServiceError(
                    f"Select at least {minimum} artists or skip onboarding",
                    code="not_enough_artists",
                )

        _validate_artist_ids(db, clean_ids)

        if skipped and not initial_onboarding:
            # A retried skip must not erase choices made later in Settings.
            diff = ExplicitPreferenceDiff(
                selected_artist_ids=selected_artist_ids(db, user_id=user.id),
                added_artist_ids=[],
                removed_artist_ids=[],
            )
        else:
            diff = replace_explicit_preferences(
                db,
                user_id=user.id,
                artist_ids=[] if skipped else clean_ids,
                source=normalized_source,
                now=timestamp,
            )

        if user.music_preferences_completed_at is None:
            user.music_preferences_completed_at = timestamp
        completed_at = _naive_utc(user.music_preferences_completed_at or timestamp)
        db.add(user)
        db.commit()
    except Exception:
        db.rollback()
        raise

    _invalidate_recommendations(user.id)
    preferences = list_user_artist_preferences(db, user_id=user.id)
    return MusicPreferenceSaveResult(
        completed_at=completed_at,
        selected_artist_ids=diff.selected_artist_ids,
        added_artist_ids=diff.added_artist_ids,
        removed_artist_ids=diff.removed_artist_ids,
        skipped=bool(skipped),
        preferences=preferences,
    )


def record_music_signal(
    db: Session,
    *,
    user_id: int,
    event_id: str,
    track_id: int,
    event_type: str,
    artist_id: int | None = None,
    position: int | None = None,
    recommendation_type: str = "unknown",
    reason: str | None = None,
    algorithm_version: str | None = None,
    context: str = "home",
    signal_type: str | None = None,
    occurred_at: datetime | None = None,
) -> MusicSignalResult:
    """Persist one recommendation interaction and apply its signal once.

    `event_id` is globally unique. Exact retries return the existing event and
    do not touch preference weights. Reusing an ID for another user, track or
    event type is rejected as a conflict.
    """

    clean_event_id = str(event_id or "").strip()
    clean_event_type = str(event_type or "").strip().lower()
    clean_recommendation_type = str(recommendation_type or "unknown").strip() or "unknown"
    clean_context = str(context or "home").strip() or "home"
    if not 8 <= len(clean_event_id) <= 128:
        raise PreferenceServiceError(
            "event_id must contain between 8 and 128 characters",
            code="invalid_event_id",
        )
    if not clean_event_type or len(clean_event_type) > 48:
        raise PreferenceServiceError("Invalid recommendation event type", code="invalid_event_type")
    if position is not None and int(position) < 0:
        raise PreferenceServiceError("Position cannot be negative", code="invalid_position")
    if len(clean_recommendation_type) > 64 or len(clean_context) > 64:
        raise PreferenceServiceError("Recommendation metadata is too long", code="invalid_metadata")

    existing = db.execute(
        select(RecommendationEvent).where(RecommendationEvent.event_id == clean_event_id)
    ).scalar_one_or_none()
    if existing is not None:
        _assert_same_event(existing, user_id=user_id, track_id=track_id, event_type=clean_event_type)
        return MusicSignalResult(event=existing, preference=None, created=False)

    user = db.get(User, int(user_id))
    if user is None:
        raise PreferenceServiceError("User not found", code="user_not_found", status_code=404)
    track = db.get(Track, int(track_id))
    if track is None:
        raise PreferenceServiceError("Track not found", code="track_not_found", status_code=404)
    resolved_artist_id = primary_artist_id_for_track(db, int(track_id))
    if artist_id is not None:
        requested_artist = db.get(Artist, int(artist_id))
        if requested_artist is None:
            raise PreferenceServiceError("Artist not found", code="artist_not_found", status_code=404)
        if (signal_type or "").strip().lower() not in {"artist_view", "follow", "hide", "unhide"}:
            linked_ids = {int(link.artist_id) for link in track.artist_links}
            if int(artist_id) not in linked_ids:
                raise PreferenceServiceError(
                    "Artist is not linked to the track",
                    code="artist_track_mismatch",
                )
        resolved_artist_id = int(artist_id)

    timestamp = _naive_utc(occurred_at or datetime.utcnow())
    event = RecommendationEvent(
        event_id=clean_event_id,
        user_id=int(user_id),
        track_id=int(track_id),
        event_type=clean_event_type,
        position=int(position) if position is not None else None,
        recommendation_type=clean_recommendation_type,
        reason=(str(reason).strip()[:255] if reason else None),
        algorithm_version=(
            str(algorithm_version or RECOMMENDATION_CONFIG.algorithm_version).strip()[:64]
            or RECOMMENDATION_CONFIG.algorithm_version
        ),
        context=clean_context,
        created_at=timestamp,
    )

    preference: UserArtistPreference | None = None
    try:
        db.add(event)
        db.flush()

        resolved_signal = (signal_type or _EVENT_SIGNAL_TYPES.get(clean_event_type) or "").strip().lower()
        delta = _signal_delta(resolved_signal)
        if resolved_artist_id is not None and delta is not None:
            preference = apply_preference_signal(
                db,
                user_id=int(user_id),
                artist_id=resolved_artist_id,
                source=resolved_signal,
                delta=delta,
                occurred_at=timestamp,
                hidden=True if resolved_signal == "hide" else None,
            )
        elif resolved_artist_id is not None and resolved_signal == "unhide":
            preference = apply_preference_signal(
                db,
                user_id=int(user_id),
                artist_id=resolved_artist_id,
                source="unhide",
                delta=0.0,
                occurred_at=timestamp,
                hidden=False,
            )

        db.commit()
        db.refresh(event)
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(RecommendationEvent).where(RecommendationEvent.event_id == clean_event_id)
        ).scalar_one_or_none()
        if existing is None:
            raise
        _assert_same_event(existing, user_id=user_id, track_id=track_id, event_type=clean_event_type)
        return MusicSignalResult(event=existing, preference=None, created=False)
    except Exception:
        db.rollback()
        raise

    if preference is not None:
        _invalidate_recommendations(int(user_id))
    return MusicSignalResult(event=event, preference=preference, created=True)


def _validate_artist_ids(db: Session, artist_ids: list[int]) -> None:
    if not artist_ids:
        return
    existing = {
        int(artist_id)
        for artist_id in db.execute(select(Artist.id).where(Artist.id.in_(artist_ids))).scalars().all()
    }
    missing = sorted(set(artist_ids) - existing)
    if missing:
        raise PreferenceServiceError(
            f"Unknown artist IDs: {', '.join(map(str, missing))}",
            code="unknown_artists",
        )


def _deduplicate_positive_ids(values: list[int] | tuple[int, ...]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw_value in values:
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as error:
            raise PreferenceServiceError("Artist IDs must be integers", code="invalid_artist_ids") from error
        if value <= 0:
            raise PreferenceServiceError("Artist IDs must be positive", code="invalid_artist_ids")
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _signal_delta(signal_type: str) -> float | None:
    if not signal_type:
        return None
    configured = {
        **SIGNAL_WEIGHTS,
        "completed_play": RECOMMENDATION_CONFIG.completed_play_weight,
        "substantial_play": RECOMMENDATION_CONFIG.substantial_play_weight,
        "repeat_play": RECOMMENDATION_CONFIG.repeat_play_weight,
        "quick_skip": RECOMMENDATION_CONFIG.quick_skip_weight,
        "repeated_quick_skip": RECOMMENDATION_CONFIG.repeated_quick_skip_weight,
    }
    value = configured.get(signal_type)
    return float(value) if value is not None else None


def _assert_same_event(
    event: RecommendationEvent,
    *,
    user_id: int,
    track_id: int,
    event_type: str,
) -> None:
    if (
        int(event.user_id) != int(user_id)
        or int(event.track_id) != int(track_id)
        or str(event.event_type) != str(event_type)
    ):
        raise PreferenceServiceError(
            "event_id is already used by another event",
            code="event_id_conflict",
            status_code=409,
        )


def _invalidate_recommendations(user_id: int) -> None:
    # Imported only after the transaction commits: recommendation_service also
    # consumes preferences, and importing it at module load would create a
    # service/repository cycle.
    try:
        from app.services.recommendation_service import invalidate_recommendations
    except ImportError:  # pragma: no cover - supports partial/legacy deployments.
        return
    invalidate_recommendations(int(user_id))


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
