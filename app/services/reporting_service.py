import csv
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.import_job import ImportJobReport
from app.models.track import Track, TrackArtist
from app.services.job_processor_service import get_artists_without_tracks, get_coverage_summary


REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def export_import_report(db: Session) -> dict[str, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    coverage = get_coverage_summary(db).model_dump()
    reports = [_report_to_dict(report) for report in db.execute(select(ImportJobReport)).scalars().all()]
    paths = {
        "coverage_json": _write_json("import_coverage_summary.json", coverage),
        "reports_json": _write_json("import_job_reports.json", reports),
        "reports_csv": _write_csv("import_job_reports.csv", reports),
    }
    return paths


def export_artists_without_tracks(db: Session) -> dict[str, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    data = get_artists_without_tracks(db, limit=10000).model_dump()
    rows = data["items"]
    return {
        "json": _write_json("artists_without_tracks.json", data),
        "csv": _write_csv("artists_without_tracks.csv", rows),
    }


def export_tracks_needs_review(db: Session) -> dict[str, str]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stmt = (
        select(Track)
        .where(Track.needs_review == True)
        .options(selectinload(Track.artist_links).selectinload(TrackArtist.artist))
        .order_by(Track.quality_score.asc(), Track.created_at.desc())
    )
    rows = [_track_to_review_dict(track) for track in db.execute(stmt).scalars().unique().all()]
    return {
        "json": _write_json("tracks_needs_review.json", rows),
        "csv": _write_csv("tracks_needs_review.csv", rows),
    }


def _report_to_dict(report: ImportJobReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "job_id": report.job_id,
        "artist_id": report.artist_id,
        "provider": report.provider,
        "fetched_count": report.fetched_count,
        "created_tracks": report.created_tracks,
        "skipped_duplicates": report.skipped_duplicates,
        "rejected_low_confidence": report.rejected_low_confidence,
        "rejected_low_quality": report.rejected_low_quality,
        "marked_needs_review": report.marked_needs_review,
        "error_message": report.error_message,
        "created_at": report.created_at.isoformat(),
    }


def _track_to_review_dict(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "title": track.title,
        "artists": ", ".join(link.artist.name for link in track.artist_links),
        "duration_seconds": track.duration_seconds,
        "genre": track.genre,
        "quality_score": track.quality_score,
        "source_name": track.source_name,
        "source_external_id": track.source_external_id,
        "source_url": track.source_url,
        "is_playable": track.is_playable,
        "audio_src": track.audio_src,
        "created_at": track.created_at.isoformat(),
    }


def _write_json(filename: str, data: Any) -> str:
    path = REPORTS_DIR / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)


def _write_csv(filename: str, rows: list[dict[str, Any]]) -> str:
    path = REPORTS_DIR / filename
    if not rows:
        path.write_text("", encoding="utf-8")
        return str(path)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return str(path)
