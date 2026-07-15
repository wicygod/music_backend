from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import yt_dlp
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.track import Track
from app.services.popular_ranking_service import PROVIDER_POPULARITY_TAG, provider_popularity_score


UNKNOWN_PROVIDER_SCORES = (50.0, 65.0, 75.0)
ProviderExtractor = Callable[[str], dict | None]


@dataclass
class PopularityRefreshResult:
    scanned: int = 0
    updated: int = 0
    unchanged: int = 0
    unavailable: int = 0
    failed: int = 0
    dry_run: bool = True
    last_track_id: int | None = None
    errors: list[str] = field(default_factory=list)


def refresh_provider_popularity(
    db: Session,
    *,
    limit: int = 20,
    after_id: int = 0,
    dry_run: bool = True,
    extractor: ProviderExtractor | None = None,
    track_ids: list[int] | None = None,
) -> PopularityRefreshResult:
    """Refresh a bounded batch of legacy placeholder popularity scores.

    The command is deliberately opt-in and bounded because it performs public
    provider requests.  `--dry-run` never changes the database and is suitable
    for checking a production batch before explicit approval.
    """

    safe_limit = max(1, min(int(limit), 200))
    result = PopularityRefreshResult(dry_run=bool(dry_run))
    scoped_ids = sorted({int(track_id) for track_id in (track_ids or []) if int(track_id) > 0})
    if track_ids is not None and not scoped_ids:
        return result
    filters = [
        Track.is_playable == True,
        Track.needs_review == False,
        Track.source_name.in_(("soundcloud", "sc", "youtube", "youtube_music", "yt")),
        Track.source_url.is_not(None),
        Track.popularity_score.in_(UNKNOWN_PROVIDER_SCORES),
        Track.id > max(0, int(after_id)),
    ]
    if track_ids is not None:
        filters.append(Track.id.in_(scoped_ids))
    tracks = list(
        db.execute(
            select(Track)
            .where(*filters)
            .order_by(Track.id.asc())
            .limit(safe_limit)
        ).scalars().all()
    )
    extract = extractor or _provider_extractor()
    for track in tracks:
        result.scanned += 1
        result.last_track_id = int(track.id)
        try:
            payload = extract(str(track.source_url or ""))
        except Exception as exc:  # provider failures must not abort the batch
            result.failed += 1
            if len(result.errors) < 10:
                result.errors.append(f"track {track.id}: {type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            result.unavailable += 1
            continue
        score, reliable = provider_popularity_score(
            view_count=payload.get("view_count"),
            like_count=payload.get("like_count"),
            repost_count=payload.get("repost_count"),
            timestamp=payload.get("timestamp"),
            fallback=float(track.popularity_score or 0.0),
        )
        if not reliable:
            result.unavailable += 1
            continue
        if abs(float(track.popularity_score or 0.0) - score) < 0.001:
            result.unchanged += 1
            continue
        result.updated += 1
        if not dry_run:
            track.popularity_score = score
            track.tags_json = _tags_with_provider_popularity(track.tags_json)
            db.add(track)

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return result


def _tags_with_provider_popularity(raw_tags: str | None) -> str:
    try:
        tags = json.loads(raw_tags or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    if not isinstance(tags, list):
        tags = []
    normalized = [str(item) for item in tags]
    if PROVIDER_POPULARITY_TAG not in normalized:
        normalized.append(PROVIDER_POPULARITY_TAG)
    return json.dumps(normalized)


def _provider_extractor() -> ProviderExtractor:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
    }
    ydl = yt_dlp.YoutubeDL(options)

    def extract(url: str) -> dict | None:
        if not url:
            return None
        payload = ydl.extract_info(url, download=False)
        return payload if isinstance(payload, dict) else None

    return extract
