from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ImportJobType = Literal["artist_seed", "track_seed", "search_query", "refresh_artist"]
ImportJobStatus = Literal["pending", "running", "done", "failed"]


class ImportJobCreate(BaseModel):
    type: ImportJobType
    payload: dict[str, Any] = Field(default_factory=dict)


class ImportJobRead(BaseModel):
    id: int
    type: str
    payload: dict[str, Any]
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class SeedLoadResult(BaseModel):
    created_tracks: int
    skipped_duplicates: int
    created_artists: int


class ArtistSeedImportResult(BaseModel):
    total_lines: int
    created_artists: int
    skipped_duplicates: int
    created_jobs: int
    skipped_existing_jobs: int
    needs_review: int


class ArtistSeedSummary(BaseModel):
    total_artists: int
    high_priority: int
    normal_priority: int
    low_priority: int
    needs_review: int
    pending_import_jobs: int


class ArtistJobProcessResult(BaseModel):
    processed_jobs: int
    done_jobs: int
    failed_jobs: int
    created_tracks: int
    linked_existing_tracks: int
    skipped_duplicates: int
    fetched_count: int = 0
    rejected_low_confidence: int = 0
    rejected_low_quality: int = 0
    marked_needs_review: int = 0
    dry_run: bool = False
    elapsed_seconds: float = 0.0
    remaining_pending_jobs: int = 0
    errors: list[str] = Field(default_factory=list)


class ImportJobsSummary(BaseModel):
    pending: int
    running: int
    done: int
    failed: int
    total: int
    imported_artists: int
    failed_artists: int
    needs_review_artists: int
    total_tracks: int


class CoverageSummary(BaseModel):
    total_artists: int
    imported_artists: int
    pending_artists: int
    failed_artists: int
    needs_review_artists: int
    artists_with_zero_tracks: int
    artists_with_1_5_tracks: int
    artists_with_6_20_tracks: int
    artists_with_20_plus_tracks: int
    total_tracks: int
    tracks_needs_review: int
    average_tracks_per_imported_artist: float


class SafeImportBatchResult(BaseModel):
    batch: int
    result: ArtistJobProcessResult


class SafeImportResult(BaseModel):
    dry_run: bool
    batch_size: int
    max_batches: int
    only_priority: str | None = None
    stopped_reason: str | None = None
    before: CoverageSummary
    after: CoverageSummary
    batches: list[SafeImportBatchResult] = Field(default_factory=list)
    totals: ArtistJobProcessResult


class ArtistWithoutTracksItem(BaseModel):
    id: int
    name: str
    normalized_name: str
    region: str
    priority: str
    needs_review: bool
    import_status: str


class ArtistsWithoutTracksResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ArtistWithoutTracksItem]
