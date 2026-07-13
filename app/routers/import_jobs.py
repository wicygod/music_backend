from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import ADMIN_API_KEY, token_matches
from app.schemas.import_job import (
    ArtistJobProcessResult,
    ArtistsWithoutTracksResponse,
    ArtistSeedImportResult,
    ArtistSeedSummary,
    CoverageSummary,
    ImportJobsSummary,
    ImportJobCreate,
    ImportJobRead,
    SafeImportResult,
    SeedLoadResult,
)
from app.services.job_processor_service import (
    get_artists_without_tracks,
    get_coverage_summary,
    get_import_jobs_summary,
    process_all_pending_artist_seed_jobs,
    process_pending_artist_seed_jobs,
    provider_test,
    reset_import_job,
    retry_failed_artist_seed_jobs,
    safe_import_artists,
)
from app.services.import_service import (
    create_import_job,
    get_artist_seed_summary,
    load_artist_seed,
    load_demo_seed,
    read_import_job,
)
from app.services.serialization_service import import_job_to_read


def require_admin_key(x_admin_key: str | None = Header(None, alias="X-Admin-Key")) -> None:
    if not token_matches(ADMIN_API_KEY, x_admin_key):
        raise HTTPException(status_code=403, detail="Forbidden")


router = APIRouter(
    prefix="/api/import",
    tags=["import"],
    dependencies=[Depends(require_admin_key)],
)


@router.post("/jobs", response_model=ImportJobRead, status_code=201)
def create_job(payload: ImportJobCreate, db: Session = Depends(get_db)) -> ImportJobRead:
    return import_job_to_read(create_import_job(db, payload))


@router.get("/jobs/summary", response_model=ImportJobsSummary)
def jobs_summary(db: Session = Depends(get_db)) -> ImportJobsSummary:
    return get_import_jobs_summary(db)


@router.get("/jobs/{job_id}", response_model=ImportJobRead)
def get_job(job_id: int, db: Session = Depends(get_db)) -> ImportJobRead:
    job = read_import_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


@router.post("/jobs/{job_id}/reset", response_model=ImportJobRead)
def reset_job(job_id: int, db: Session = Depends(get_db)) -> ImportJobRead:
    job = reset_import_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return import_job_to_read(job)


@router.post("/demo-seed", response_model=SeedLoadResult)
def load_demo(db: Session = Depends(get_db)) -> SeedLoadResult:
    return load_demo_seed(db)


@router.post("/seed-artists", response_model=ArtistSeedImportResult)
def seed_artists(db: Session = Depends(get_db)) -> ArtistSeedImportResult:
    return load_artist_seed(db)


@router.get("/seed-artists/summary", response_model=ArtistSeedSummary)
def seed_artists_summary(db: Session = Depends(get_db)) -> ArtistSeedSummary:
    return get_artist_seed_summary(db)


@router.post("/process-artist-jobs", response_model=ArtistJobProcessResult)
def process_artist_jobs(
    limit: int = Query(5, ge=1, le=100),
    dry_run: bool = Query(False),
    db: Session = Depends(get_db),
) -> ArtistJobProcessResult:
    return process_pending_artist_seed_jobs(db, limit=limit, dry_run=dry_run)


@router.post("/process-all-artist-jobs", response_model=ArtistJobProcessResult)
def process_all_artist_jobs(dry_run: bool = Query(False), db: Session = Depends(get_db)) -> ArtistJobProcessResult:
    return process_all_pending_artist_seed_jobs(db, dry_run=dry_run)


@router.post("/safe-import-artists", response_model=SafeImportResult)
def safe_import_artists_endpoint(
    batch_size: int = Query(10, ge=1, le=100),
    max_batches: int = Query(5, ge=1, le=50),
    dry_run: bool = Query(False),
    stop_on_high_failure_rate: bool = Query(True),
    only_priority: str | None = Query(None),
    db: Session = Depends(get_db),
) -> SafeImportResult:
    return safe_import_artists(
        db,
        batch_size=batch_size,
        max_batches=max_batches,
        dry_run=dry_run,
        stop_on_high_failure_rate=stop_on_high_failure_rate,
        only_priority=only_priority,
    )


@router.get("/provider-test")
def test_provider(
    artist: str = Query(..., min_length=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return {"artist": artist, "results": provider_test(artist, limit=limit, db=db)}


@router.post("/retry-failed-artist-jobs")
def retry_failed_jobs(limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)) -> dict[str, int]:
    return retry_failed_artist_seed_jobs(db, limit=limit)


@router.get("/coverage-summary", response_model=CoverageSummary)
def coverage_summary(db: Session = Depends(get_db)) -> CoverageSummary:
    return get_coverage_summary(db)


@router.get("/artists-without-tracks", response_model=ArtistsWithoutTracksResponse)
def artists_without_tracks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    priority: str | None = Query(None),
    needs_review: bool | None = Query(None),
    db: Session = Depends(get_db),
) -> ArtistsWithoutTracksResponse:
    return get_artists_without_tracks(
        db,
        limit=limit,
        offset=offset,
        priority=priority,
        needs_review=needs_review,
    )
