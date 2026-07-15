from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import case, create_engine, or_, select
from sqlalchemy.orm import Session, selectinload


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.models.artist import Artist  # noqa: E402
from app.models.track import TrackArtist  # noqa: E402
from app.services.normalization_service import normalize_artist_name  # noqa: E402
from app.services.canonical_artist_service import apply_canonical_profile  # noqa: E402
from app.services.soundcloud_profile_service import (  # noqa: E402
    SoundCloudProfile,
    resolve_canonical_soundcloud_profile,
)


DEFAULT_DATABASE_URL = os.getenv("MUSIC_DATABASE_URL", "sqlite:///./music_catalog.db")
DEFAULT_LIMIT = 50
DEFAULT_WORKERS = 4
MAX_LIMIT = 2_000
MAX_WORKERS = 8
PROFILE_CANDIDATE_LIMIT = 12
PROFILE_RESOLUTION_TIMEOUT_SECONDS = 12.0

_RESERVED_SOUNDCLOUD_PATHS = {
    "charts",
    "discover",
    "messages",
    "search",
    "settings",
    "stream",
    "upload",
    "you",
}

ProfileResolver = Callable[..., SoundCloudProfile | None]


@dataclass(slots=True)
class BackfillSummary:
    dry_run: bool
    scanned: int = 0
    resolved: int = 0
    updated: int = 0
    skipped_no_profile: int = 0
    skipped_non_exact: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def soundcloud_profile_root(value: str | None) -> str | None:
    """Return a public SoundCloud profile root for a profile or track URL."""

    if not value:
        return None
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or host != "soundcloud.com":
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or path_parts[0].lower() in _RESERVED_SOUNDCLOUD_PATHS:
        return None
    return urlunsplit(("https", "soundcloud.com", f"/{path_parts[0]}", "", ""))


def collect_candidate_profile_urls(artist: Artist) -> list[str]:
    """Collect stable uploader roots without treating artwork as an avatar."""

    raw_candidates: list[str | None] = [artist.source_url]
    if soundcloud_profile_root(artist.avatar_url) is not None:
        raw_candidates.append(artist.avatar_url)

    for link in sorted(artist.track_links, key=lambda item: (item.track_id, item.role)):
        track = getattr(link, "track", None)
        if track is not None:
            raw_candidates.append(track.source_url)

    candidates: list[str] = []
    seen: set[str] = set()
    for value in raw_candidates:
        profile_url = soundcloud_profile_root(value)
        if profile_url is None or profile_url in seen:
            continue
        seen.add(profile_url)
        candidates.append(profile_url)
    return candidates


def backfill_canonical_artists(
    db: Session,
    *,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    resolver: ProfileResolver = resolve_canonical_soundcloud_profile,
    resolved_at: datetime | None = None,
    workers: int = 1,
    include_imported_profiles: bool = False,
) -> BackfillSummary:
    """Resolve exact SoundCloud identities for seed artists without merging rows."""

    safe_limit = max(1, min(int(limit), MAX_LIMIT))
    normalized_query = normalize_artist_name(query or "")
    priority_order = case(
        (Artist.priority == "high", 0),
        (Artist.priority == "normal", 1),
        else_=2,
    )
    source_filter = or_(Artist.seed_source == "artist_seed", Artist.source_name == "artist_seed")
    if include_imported_profiles:
        source_filter = or_(
            source_filter,
            Artist.source_name.in_(("soundcloud", "sc")),
            Artist.source_url.like("%soundcloud.com/%"),
            Artist.avatar_url.like("%soundcloud.com/%"),
        )
    stmt = (
        select(Artist)
        .where(source_filter)
        .options(selectinload(Artist.track_links).selectinload(TrackArtist.track))
        .order_by(priority_order, Artist.id.asc())
        .limit(safe_limit)
    )
    if normalized_query:
        stmt = stmt.where(Artist.normalized_name.contains(normalized_query, autoescape=True))

    artists = list(db.execute(stmt).scalars().unique().all())
    summary = BackfillSummary(dry_run=bool(dry_run))
    timestamp = _naive_utc(resolved_at or datetime.now(timezone.utc))

    safe_workers = max(1, min(int(workers), MAX_WORKERS))
    work_items = [
        (
            artist,
            collect_candidate_profile_urls(artist),
            bool(artist.seed_source == "artist_seed" or artist.source_name == "artist_seed"),
        )
        for artist in artists
    ]
    summary.scanned = len(work_items)
    profiles_by_artist: dict[int, SoundCloudProfile | None | Exception] = {}
    if safe_workers == 1:
        for artist, candidate_urls, search_for_best_profile in work_items:
            try:
                profiles_by_artist[artist.id] = _resolve_profile(
                    artist.name,
                    candidate_urls,
                    resolver,
                    search_for_best_profile,
                )
            except Exception as exc:  # noqa: BLE001 - one provider failure must not abort the batch
                profiles_by_artist[artist.id] = exc
    else:
        with ThreadPoolExecutor(max_workers=safe_workers, thread_name_prefix="artist-backfill") as pool:
            future_to_artist = {
                pool.submit(
                    _resolve_profile,
                    artist.name,
                    candidate_urls,
                    resolver,
                    search_for_best_profile,
                ): artist.id
                for artist, candidate_urls, search_for_best_profile in work_items
            }
            for future in as_completed(future_to_artist):
                artist_id = future_to_artist[future]
                try:
                    profiles_by_artist[artist_id] = future.result()
                except Exception as exc:  # noqa: BLE001 - one provider failure must not abort the batch
                    profiles_by_artist[artist_id] = exc

    for artist, _candidate_urls, _search_for_best_profile in work_items:
        profile = profiles_by_artist.get(artist.id)
        if isinstance(profile, Exception):
            summary.failed += 1
            summary.errors.append(f"artist {artist.id}: {type(profile).__name__}: {profile}")
            continue

        if profile is None:
            summary.skipped_no_profile += 1
            continue
        summary.resolved += 1

        expected_name = normalize_artist_name(artist.name)
        resolved_name = normalize_artist_name(profile.username)
        if not expected_name or resolved_name != expected_name:
            summary.skipped_non_exact += 1
            continue

        apply_canonical_profile(artist, profile, resolved_at=timestamp)
        summary.updated += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return summary


def _resolve_profile(
    artist_name: str,
    candidate_urls: list[str],
    resolver: ProfileResolver,
    search_for_best_profile: bool = True,
) -> SoundCloudProfile | None:
    # One combined resolution compares bounded search results with the
    # catalog's known profile roots. A prior two-pass flow could resolve a good
    # direct profile first and then overwrite it with a weaker search-only
    # result from a second, inconsistent provider snapshot.
    return resolver(
        artist_name,
        candidate_urls,
        include_search=search_for_best_profile or not candidate_urls,
        max_candidates=PROFILE_CANDIDATE_LIMIT,
        timeout=PROFILE_RESOLUTION_TIMEOUT_SECONDS,
    )


def run_backfill(
    database_url: str,
    *,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    resolver: ProfileResolver = resolve_canonical_soundcloud_profile,
    workers: int = DEFAULT_WORKERS,
    include_imported_profiles: bool = False,
) -> BackfillSummary:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, future=True)
    try:
        with Session(engine) as db:
            return backfill_canonical_artists(
                db,
                query=query,
                limit=limit,
                dry_run=dry_run,
                resolver=resolver,
                workers=workers,
                include_imported_profiles=include_imported_profiles,
            )
    finally:
        engine.dispose()


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if parsed < 1 or parsed > MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_LIMIT}")
    return parsed


def _worker_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be an integer") from exc
    if parsed < 1 or parsed > MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"workers must be between 1 and {MAX_WORKERS}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill exact canonical SoundCloud profiles for seeded artists",
    )
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--query", default=None, help="Process seeded artists whose normalized name contains this text")
    parser.add_argument("--limit", type=_positive_limit, default=DEFAULT_LIMIT)
    parser.add_argument("--workers", type=_worker_count, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--include-imported",
        action="store_true",
        help="Also validate existing SoundCloud uploader profiles",
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve profiles and report changes without committing")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    summary = run_backfill(
        args.database_url,
        query=args.query,
        limit=args.limit,
        dry_run=args.dry_run,
        workers=args.workers,
        include_imported_profiles=args.include_imported,
    )
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
