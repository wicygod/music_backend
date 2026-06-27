import json
import time
from dataclasses import asdict
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import IMPORT_BATCH_LIMIT, IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT, PROVIDER_REQUEST_DELAY_SECONDS
from app.models.artist import Artist
from app.models.import_job import ImportJob, ImportJobReport
from app.models.track import Track, TrackArtist
from app.providers.base import ProviderTrackResult
from app.providers.manager import ProviderManager
from app.repositories.tracks import (
    ensure_track_artist_link,
    find_duplicate_track_for_artist,
    find_track_by_provider_external_id,
)
from app.schemas.import_job import (
    ArtistJobProcessResult,
    ArtistsWithoutTracksResponse,
    ArtistWithoutTracksItem,
    CoverageSummary,
    ImportJobsSummary,
    SafeImportBatchResult,
    SafeImportResult,
)
from app.services.normalization_service import detect_artist_region, normalize_artist_name, normalize_title
from app.services.serialization_service import parse_json_object


TITLE_PENALTIES = {
    "karaoke": 25,
    "tribute": 25,
    "cover": 20,
    "instrumental": 20,
    "type beat": 20,
    "sped up": 15,
    "slowed": 15,
    "reverb": 15,
    "nightcore": 15,
    "live": 10,
    "remix": 10,
}


def provider_test(artist_name: str, limit: int = 25, db: Session | None = None) -> list[dict]:
    manager = ProviderManager()
    tracks = manager.search_tracks_by_artist(artist_name, limit=limit)
    return [evaluate_provider_track(db, artist_name, None, track) for track in tracks]


def process_pending_artist_seed_jobs(
    db: Session,
    limit: int = IMPORT_BATCH_LIMIT,
    *,
    dry_run: bool = False,
    pause_seconds: float = PROVIDER_REQUEST_DELAY_SECONDS,
    only_priority: str | None = None,
) -> ArtistJobProcessResult:
    started_at = time.perf_counter()
    jobs = _pending_artist_seed_jobs(db, only_priority=only_priority)
    if limit > 0:
        jobs = jobs[:limit]

    manager = ProviderManager()
    result = _empty_process_result(dry_run=dry_run)

    for job in jobs:
        result.processed_jobs += 1
        try:
            payload = parse_json_object(job.payload_json)
            artist_id = int(payload["artist_id"])
            artist_name = str(payload["artist_name"])
            artist = db.get(Artist, artist_id)
            if not artist:
                raise ValueError(f"Artist {artist_id} not found")

            if not dry_run:
                job.status = "running"
                job.error_message = None
                db.commit()

            tracks_target = max(
                1,
                min(int(getattr(artist, "tracks_target", 25) or 25), IMPORT_MAX_TRACKS_PER_ARTIST_HARD_LIMIT),
            )
            provider_tracks = manager.search_tracks_by_artist(artist_name, limit=tracks_target)
            save_stats = _save_provider_tracks(db, artist, provider_tracks, dry_run=dry_run)
            _merge_stats(result, save_stats)

            if not dry_run:
                _write_import_report(db, job, artist, save_stats)
                if save_stats["saved_or_linked"] == 0:
                    artist.import_status = "failed"
                    job.status = "failed"
                    job.error_message = "No acceptable metadata tracks found"
                    result.failed_jobs += 1
                else:
                    if save_stats["marked_needs_review"] > save_stats["saved_or_linked"] / 2:
                        artist.import_status = "needs_review"
                        artist.needs_review = True
                    else:
                        artist.import_status = "imported"
                    artist.last_imported_at = datetime.utcnow()
                    job.status = "done"
                    result.done_jobs += 1
                db.commit()
            else:
                if save_stats["saved_or_linked"] > 0:
                    result.done_jobs += 1
                else:
                    result.failed_jobs += 1
                db.rollback()
        except Exception as exc:  # noqa: BLE001 - batch import must survive one bad artist
            db.rollback()
            if not dry_run:
                job = db.get(ImportJob, job.id)
                if job:
                    payload = parse_json_object(job.payload_json)
                    artist = db.get(Artist, int(payload.get("artist_id", 0))) if payload.get("artist_id") else None
                    job.status = "failed"
                    job.error_message = str(exc)
                    if artist:
                        artist.import_status = "failed"
                        _write_import_report(db, job, artist, {"error_message": str(exc)})
                    db.commit()
            result.failed_jobs += 1
            result.errors.append(f"job {getattr(job, 'id', 'unknown')}: {exc}")

        if pause_seconds > 0:
            time.sleep(pause_seconds)

    result.elapsed_seconds = round(time.perf_counter() - started_at, 3)
    result.remaining_pending_jobs = len(_pending_artist_seed_jobs(db, only_priority=only_priority))
    return result


def process_all_pending_artist_seed_jobs(
    db: Session,
    *,
    dry_run: bool = False,
    pause_seconds: float = PROVIDER_REQUEST_DELAY_SECONDS,
    only_priority: str | None = None,
) -> ArtistJobProcessResult:
    if dry_run:
        return process_pending_artist_seed_jobs(
            db,
            limit=0,
            dry_run=True,
            pause_seconds=pause_seconds,
            only_priority=only_priority,
        )

    total = _empty_process_result(dry_run=dry_run)
    while True:
        batch = process_pending_artist_seed_jobs(
            db,
            limit=IMPORT_BATCH_LIMIT,
            dry_run=dry_run,
            pause_seconds=pause_seconds,
            only_priority=only_priority,
        )
        if batch.processed_jobs == 0:
            return total
        _merge_result(total, batch)
        total.remaining_pending_jobs = batch.remaining_pending_jobs


def safe_import_artists(
    db: Session,
    *,
    batch_size: int = 10,
    max_batches: int = 5,
    dry_run: bool = False,
    stop_on_high_failure_rate: bool = True,
    only_priority: str | None = None,
    pause_seconds: float = PROVIDER_REQUEST_DELAY_SECONDS,
) -> SafeImportResult:
    before = get_coverage_summary(db)
    totals = _empty_process_result(dry_run=dry_run)
    batches: list[SafeImportBatchResult] = []
    stopped_reason: str | None = None
    bad_batches = 0

    if dry_run:
        batch = process_pending_artist_seed_jobs(
            db,
            limit=batch_size * max_batches,
            dry_run=True,
            pause_seconds=pause_seconds,
            only_priority=only_priority,
        )
        _merge_result(totals, batch)
        totals.remaining_pending_jobs = batch.remaining_pending_jobs
        batches.append(SafeImportBatchResult(batch=1, result=batch))
        return SafeImportResult(
            dry_run=True,
            batch_size=batch_size,
            max_batches=max_batches,
            only_priority=only_priority,
            stopped_reason="dry_run_completed",
            before=before,
            after=get_coverage_summary(db),
            batches=batches,
            totals=totals,
        )

    for batch_index in range(1, max_batches + 1):
        batch = process_pending_artist_seed_jobs(
            db,
            limit=batch_size,
            dry_run=dry_run,
            pause_seconds=pause_seconds,
            only_priority=only_priority,
        )
        if batch.processed_jobs == 0:
            stopped_reason = "no_pending_jobs"
            break

        _merge_result(totals, batch)
        totals.remaining_pending_jobs = batch.remaining_pending_jobs
        batches.append(SafeImportBatchResult(batch=batch_index, result=batch))

        if stop_on_high_failure_rate and _is_bad_batch(batch):
            bad_batches += 1
            if bad_batches >= 2:
                stopped_reason = "stopped_on_high_failure_or_rejection_rate"
                break
        else:
            bad_batches = 0

        if batch.remaining_pending_jobs == 0:
            stopped_reason = "no_pending_jobs"
            break

    if stopped_reason is None:
        stopped_reason = "max_batches_reached"
    totals.elapsed_seconds = round(sum(item.result.elapsed_seconds for item in batches), 3)

    return SafeImportResult(
        dry_run=dry_run,
        batch_size=batch_size,
        max_batches=max_batches,
        only_priority=only_priority,
        stopped_reason=stopped_reason,
        before=before,
        after=get_coverage_summary(db),
        batches=batches,
        totals=totals,
    )


def retry_failed_artist_seed_jobs(db: Session, limit: int = 10) -> dict[str, int]:
    jobs = list(
        db.execute(
            select(ImportJob)
            .where(ImportJob.type == "artist_seed", ImportJob.status == "failed")
            .order_by(ImportJob.updated_at.asc(), ImportJob.id.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.status = "pending"
        job.error_message = None
    db.commit()
    return {"reset_jobs": len(jobs)}


def reset_import_job(db: Session, job_id: int) -> ImportJob | None:
    job = db.get(ImportJob, job_id)
    if not job:
        return None
    job.status = "pending"
    job.error_message = None
    db.commit()
    db.refresh(job)
    return job


def get_import_jobs_summary(db: Session) -> ImportJobsSummary:
    def job_count(status: str) -> int:
        return db.execute(select(func.count()).select_from(ImportJob).where(ImportJob.status == status)).scalar_one()

    return ImportJobsSummary(
        pending=job_count("pending"),
        running=job_count("running"),
        done=job_count("done"),
        failed=job_count("failed"),
        total=db.execute(select(func.count()).select_from(ImportJob)).scalar_one(),
        imported_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.import_status == "imported")).scalar_one(),
        failed_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.import_status == "failed")).scalar_one(),
        needs_review_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.needs_review == True)).scalar_one(),
        total_tracks=db.execute(select(func.count()).select_from(Track)).scalar_one(),
    )


def get_coverage_summary(db: Session) -> CoverageSummary:
    counts_by_artist = _track_counts_by_artist(db)
    total_artists = db.execute(select(func.count()).select_from(Artist)).scalar_one()
    imported_artists = db.execute(select(func.count()).select_from(Artist).where(Artist.import_status == "imported")).scalar_one()
    zero = total_artists - len(counts_by_artist)
    one_to_five = sum(1 for count in counts_by_artist.values() if 1 <= count <= 5)
    six_to_twenty = sum(1 for count in counts_by_artist.values() if 6 <= count <= 20)
    twenty_plus = sum(1 for count in counts_by_artist.values() if count > 20)
    total_tracks = db.execute(select(func.count()).select_from(Track)).scalar_one()
    return CoverageSummary(
        total_artists=total_artists,
        imported_artists=imported_artists,
        pending_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.import_status == "pending")).scalar_one(),
        failed_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.import_status == "failed")).scalar_one(),
        needs_review_artists=db.execute(select(func.count()).select_from(Artist).where(Artist.needs_review == True)).scalar_one(),
        artists_with_zero_tracks=zero,
        artists_with_1_5_tracks=one_to_five,
        artists_with_6_20_tracks=six_to_twenty,
        artists_with_20_plus_tracks=twenty_plus,
        total_tracks=total_tracks,
        tracks_needs_review=db.execute(select(func.count()).select_from(Track).where(Track.needs_review == True)).scalar_one(),
        average_tracks_per_imported_artist=round(total_tracks / imported_artists, 2) if imported_artists else 0.0,
    )


def get_artists_without_tracks(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    priority: str | None = None,
    needs_review: bool | None = None,
) -> ArtistsWithoutTracksResponse:
    counts_by_artist = _track_counts_by_artist(db)
    artist_ids_with_tracks = set(counts_by_artist)
    stmt = select(Artist)
    count_stmt = select(func.count()).select_from(Artist)
    filters = [~Artist.id.in_(artist_ids_with_tracks)] if artist_ids_with_tracks else []
    if priority:
        filters.append(Artist.priority == priority)
    if needs_review is not None:
        filters.append(Artist.needs_review == needs_review)
    for filter_item in filters:
        stmt = stmt.where(filter_item)
        count_stmt = count_stmt.where(filter_item)
    total = db.execute(count_stmt).scalar_one()
    artists = db.execute(stmt.order_by(Artist.priority.asc(), Artist.name.asc()).limit(limit).offset(offset)).scalars().all()
    return ArtistsWithoutTracksResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            ArtistWithoutTracksItem(
                id=artist.id,
                name=artist.name,
                normalized_name=artist.normalized_name,
                region=artist.region,
                priority=artist.priority,
                needs_review=artist.needs_review,
                import_status=artist.import_status,
            )
            for artist in artists
        ],
    )


def evaluate_provider_track(
    db: Session | None,
    seed_artist_name: str,
    artist: Artist | None,
    track: ProviderTrackResult,
) -> dict:
    artist_match_score = calculate_artist_match_score(seed_artist_name, track.artist_name)
    quality_score = calculate_quality_score(track)
    strict_artist = bool(artist and artist.needs_review)
    decision = _decision_for_scores(artist_match_score, quality_score, strict_artist)
    duplicate = False
    provider_name = track.raw.get("_provider", "metadata")
    if db is not None:
        duplicate = bool(find_track_by_provider_external_id(db, provider=provider_name, external_id=track.external_id))
        normalized_artist = artist.normalized_name if artist else normalize_artist_name(seed_artist_name)
        duplicate = duplicate or bool(
            find_duplicate_track_for_artist(
                db,
                normalized_artist=normalized_artist,
                title=track.title,
                duration_seconds=track.duration_seconds,
            )
        )
    if duplicate and decision.startswith("would_save"):
        decision = "duplicate_if_known"
    return {
        **asdict(track),
        "artist_match_score": artist_match_score,
        "quality_score": quality_score,
        "cover": bool(track.cover_url),
        "duplicate_if_known": duplicate,
        "decision": decision,
    }


def calculate_artist_match_score(seed_artist_name: str, result_artist_name: str) -> float:
    seed = normalize_artist_name(seed_artist_name)
    result = normalize_artist_name(result_artist_name)
    if not seed or not result:
        return 0.0
    if seed == result:
        return 1.0
    seed_tokens = seed.split()
    result_tokens = result.split()
    if seed in result or result in seed:
        return 0.88 if seed_tokens == result_tokens[: len(seed_tokens)] or result_tokens == seed_tokens[: len(result_tokens)] else 0.82
    seq_score = SequenceMatcher(None, seed, result).ratio()
    token_overlap = len(set(seed_tokens) & set(result_tokens)) / max(len(set(seed_tokens)), 1)
    ordered_bonus = 0.05 if seed_tokens and result_tokens and seed_tokens[0] == result_tokens[0] else 0.0
    return round(min(1.0, max(seq_score, token_overlap) + ordered_bonus), 3)


def calculate_quality_score(track: ProviderTrackResult) -> float:
    score = 0
    if track.title:
        score += 30
    if track.artist_name:
        score += 20
    if track.duration_seconds:
        score += 15
    if track.cover_url:
        score += 20
    if track.genre:
        score += 10
    if track.source_url:
        score += 10
    title = (track.title or "").lower()
    for word, penalty in TITLE_PENALTIES.items():
        if word in title:
            score -= penalty
    return max(0, min(100, float(score)))


def _save_provider_tracks(
    db: Session,
    artist: Artist,
    tracks: list[ProviderTrackResult],
    *,
    dry_run: bool,
) -> dict[str, int | str | None]:
    stats: dict[str, int | str | None] = {
        "fetched_count": len(tracks),
        "created_tracks": 0,
        "linked_existing_tracks": 0,
        "skipped_duplicates": 0,
        "rejected_low_confidence": 0,
        "rejected_low_quality": 0,
        "marked_needs_review": 0,
        "saved_or_linked": 0,
        "error_message": None,
        "provider": tracks[0].raw.get("_provider", "metadata") if tracks else "metadata",
    }
    for provider_track in tracks:
        evaluation = evaluate_provider_track(db, artist.name, artist, provider_track)
        decision = evaluation["decision"]
        if decision == "would_reject_low_confidence":
            stats["rejected_low_confidence"] = int(stats["rejected_low_confidence"]) + 1
            continue
        if decision == "would_reject_low_quality":
            stats["rejected_low_quality"] = int(stats["rejected_low_quality"]) + 1
            continue
        if decision == "duplicate_if_known":
            duplicate = _find_duplicate_for_provider_track(db, artist, provider_track)
            if duplicate:
                if not dry_run:
                    ensure_track_artist_link(db, track_id=duplicate.id, artist_id=artist.id)
                stats["linked_existing_tracks"] = int(stats["linked_existing_tracks"]) + 1
            stats["skipped_duplicates"] = int(stats["skipped_duplicates"]) + 1
            stats["saved_or_linked"] = int(stats["saved_or_linked"]) + 1
            continue
        needs_review = decision == "would_save_needs_review"
        if needs_review:
            stats["marked_needs_review"] = int(stats["marked_needs_review"]) + 1
        if dry_run:
            stats["created_tracks"] = int(stats["created_tracks"]) + 1
            stats["saved_or_linked"] = int(stats["saved_or_linked"]) + 1
            continue
        track = Track(
            title=provider_track.title.strip(),
            normalized_title=normalize_title(provider_track.title),
            duration_seconds=provider_track.duration_seconds or 0,
            cover_url=provider_track.cover_url,
            genre=provider_track.genre,
            tags_json=json.dumps(["provider", provider_track.raw.get("_provider", "metadata")]),
            language=None,
            region=artist.region if artist.region in {"ru", "global"} else detect_artist_region(provider_track.artist_name),
            popularity_score=provider_track.popularity_score,
            quality_score=float(evaluation["quality_score"]),
            is_playable=False,
            audio_src=None,
            source_name=provider_track.raw.get("_provider", "metadata"),
            source_external_id=provider_track.external_id,
            source_url=provider_track.source_url,
            needs_review=needs_review,
        )
        db.add(track)
        db.flush()
        ensure_track_artist_link(db, track_id=track.id, artist_id=artist.id)
        stats["created_tracks"] = int(stats["created_tracks"]) + 1
        stats["saved_or_linked"] = int(stats["saved_or_linked"]) + 1
    return stats


def _decision_for_scores(artist_match_score: float, quality_score: float, strict_artist: bool) -> str:
    normal_threshold = 0.8 if strict_artist else 0.75
    review_threshold = 0.65 if strict_artist else 0.55
    if artist_match_score < review_threshold:
        return "would_reject_low_confidence"
    if quality_score < 45:
        return "would_reject_low_quality"
    if artist_match_score < normal_threshold or quality_score < 70:
        return "would_save_needs_review"
    return "would_save"


def _find_duplicate_for_provider_track(db: Session, artist: Artist, track: ProviderTrackResult) -> Track | None:
    provider_name = track.raw.get("_provider", "metadata")
    return find_track_by_provider_external_id(db, provider=provider_name, external_id=track.external_id) or find_duplicate_track_for_artist(
        db,
        normalized_artist=artist.normalized_name,
        title=track.title,
        duration_seconds=track.duration_seconds,
    )


def _write_import_report(db: Session, job: ImportJob, artist: Artist, stats: dict) -> None:
    db.add(
        ImportJobReport(
            job_id=job.id,
            artist_id=artist.id,
            provider=str(stats.get("provider") or "metadata"),
            fetched_count=int(stats.get("fetched_count") or 0),
            created_tracks=int(stats.get("created_tracks") or 0),
            skipped_duplicates=int(stats.get("skipped_duplicates") or 0),
            rejected_low_confidence=int(stats.get("rejected_low_confidence") or 0),
            rejected_low_quality=int(stats.get("rejected_low_quality") or 0),
            marked_needs_review=int(stats.get("marked_needs_review") or 0),
            error_message=stats.get("error_message"),
        )
    )


def _pending_artist_seed_jobs(db: Session, *, only_priority: str | None = None) -> list[ImportJob]:
    jobs = list(
        db.execute(select(ImportJob).where(ImportJob.type == "artist_seed", ImportJob.status == "pending")).scalars().all()
    )
    if only_priority:
        jobs = [job for job in jobs if parse_json_object(job.payload_json).get("priority") == only_priority]
    priority_order = {"high": 0, "normal": 1, "low": 2, "unknown": 3}
    return sorted(
        jobs,
        key=lambda job: (
            priority_order.get(parse_json_object(job.payload_json).get("priority", "normal"), 1),
            job.created_at,
            job.id,
        ),
    )


def _track_counts_by_artist(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(TrackArtist.artist_id, func.count(TrackArtist.track_id)).group_by(TrackArtist.artist_id)
    ).all()
    return {int(artist_id): int(count) for artist_id, count in rows}


def _empty_process_result(*, dry_run: bool) -> ArtistJobProcessResult:
    return ArtistJobProcessResult(
        processed_jobs=0,
        done_jobs=0,
        failed_jobs=0,
        created_tracks=0,
        linked_existing_tracks=0,
        skipped_duplicates=0,
        dry_run=dry_run,
    )


def _merge_stats(result: ArtistJobProcessResult, stats: dict) -> None:
    result.created_tracks += int(stats.get("created_tracks") or 0)
    result.linked_existing_tracks += int(stats.get("linked_existing_tracks") or 0)
    result.skipped_duplicates += int(stats.get("skipped_duplicates") or 0)
    result.fetched_count += int(stats.get("fetched_count") or 0)
    result.rejected_low_confidence += int(stats.get("rejected_low_confidence") or 0)
    result.rejected_low_quality += int(stats.get("rejected_low_quality") or 0)
    result.marked_needs_review += int(stats.get("marked_needs_review") or 0)


def _merge_result(total: ArtistJobProcessResult, batch: ArtistJobProcessResult) -> None:
    total.processed_jobs += batch.processed_jobs
    total.done_jobs += batch.done_jobs
    total.failed_jobs += batch.failed_jobs
    total.created_tracks += batch.created_tracks
    total.linked_existing_tracks += batch.linked_existing_tracks
    total.skipped_duplicates += batch.skipped_duplicates
    total.fetched_count += batch.fetched_count
    total.rejected_low_confidence += batch.rejected_low_confidence
    total.rejected_low_quality += batch.rejected_low_quality
    total.marked_needs_review += batch.marked_needs_review
    total.elapsed_seconds = round(total.elapsed_seconds + batch.elapsed_seconds, 3)
    total.errors.extend(batch.errors)


def _is_bad_batch(batch: ArtistJobProcessResult) -> bool:
    if batch.processed_jobs <= 0:
        return False
    failure_rate = batch.failed_jobs / batch.processed_jobs
    rejection_rate = batch.rejected_low_confidence / batch.fetched_count if batch.fetched_count else 0
    return failure_rate >= 0.4 or rejection_rate >= 0.85
