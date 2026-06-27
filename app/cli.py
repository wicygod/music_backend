import argparse
import sys

from app.database import SessionLocal, init_db
from app.services.import_service import load_artist_seed, load_demo_seed
from app.services.job_processor_service import (
    get_coverage_summary,
    process_all_pending_artist_seed_jobs,
    process_pending_artist_seed_jobs,
    provider_test,
    reset_import_job,
    retry_failed_artist_seed_jobs,
    safe_import_artists,
)
from app.services.reporting_service import (
    export_artists_without_tracks,
    export_import_report,
    export_tracks_needs_review,
)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Million Dollars Music backend tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create database tables")
    subparsers.add_parser("seed-demo", help="Load demo metadata-only tracks")
    subparsers.add_parser("seed-artists", help="Load artist seed list and create artist import jobs")
    process_parser = subparsers.add_parser("process-artist-jobs", help="Process pending artist_seed jobs")
    process_parser.add_argument("--limit", type=int, default=5)
    process_parser.add_argument("--dry-run", action="store_true")
    process_all_parser = subparsers.add_parser("process-all-artist-jobs", help="Process all pending artist_seed jobs")
    process_all_parser.add_argument("--dry-run", action="store_true")
    safe_parser = subparsers.add_parser("safe-import-artists", help="Controlled artist import runner")
    safe_parser.add_argument("--batch-size", type=int, default=10)
    safe_parser.add_argument("--max-batches", type=int, default=5)
    safe_parser.add_argument("--stop-on-high-failure-rate", action="store_true", default=False)
    safe_parser.add_argument("--dry-run", action="store_true")
    safe_parser.add_argument("--only-priority", choices=["high", "normal", "low", "unknown"], default=None)
    provider_parser = subparsers.add_parser("provider-test", help="Search provider tracks without saving")
    provider_parser.add_argument("artist")
    provider_parser.add_argument("--limit", type=int, default=25)
    retry_parser = subparsers.add_parser("retry-failed-artist-jobs", help="Reset failed artist_seed jobs to pending")
    retry_parser.add_argument("--limit", type=int, default=10)
    reset_parser = subparsers.add_parser("reset-import-job", help="Reset one import job to pending")
    reset_parser.add_argument("job_id", type=int)
    subparsers.add_parser("import-coverage-summary", help="Print import coverage summary")
    subparsers.add_parser("export-import-report", help="Export coverage and import job reports")
    subparsers.add_parser("export-artists-without-tracks", help="Export artists without tracks")
    subparsers.add_parser("export-tracks-needs-review", help="Export tracks marked needs_review")
    args = parser.parse_args()

    init_db()
    if args.command == "init-db":
        print("Database tables are ready.")
        return

    if args.command == "seed-demo":
        with SessionLocal() as db:
            result = load_demo_seed(db)
        print(
            "Demo seed loaded: "
            f"created_tracks={result.created_tracks}, "
            f"skipped_duplicates={result.skipped_duplicates}, "
            f"created_artists={result.created_artists}"
        )
        return

    if args.command == "seed-artists":
        with SessionLocal() as db:
            result = load_artist_seed(db)
        print("Artist seed import complete:")
        print(f"- total_lines: {result.total_lines}")
        print(f"- created_artists: {result.created_artists}")
        print(f"- skipped_duplicates: {result.skipped_duplicates}")
        print(f"- created_jobs: {result.created_jobs}")
        print(f"- skipped_existing_jobs: {result.skipped_existing_jobs}")
        print(f"- needs_review: {result.needs_review}")
        return

    if args.command == "process-artist-jobs":
        with SessionLocal() as db:
            result = process_pending_artist_seed_jobs(db, limit=args.limit, dry_run=args.dry_run)
        print("Artist job processing complete:")
        _print_process_result(result)
        if result.errors:
            print("- errors:")
            for error in result.errors:
                print(f"  - {error}")
        return

    if args.command == "process-all-artist-jobs":
        with SessionLocal() as db:
            result = process_all_pending_artist_seed_jobs(db, dry_run=args.dry_run)
        print("All pending artist jobs processed:")
        _print_process_result(result)
        if result.errors:
            print("- errors:")
            for error in result.errors:
                print(f"  - {error}")
        return

    if args.command == "safe-import-artists":
        with SessionLocal() as db:
            result = safe_import_artists(
                db,
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                dry_run=args.dry_run,
                stop_on_high_failure_rate=args.stop_on_high_failure_rate,
                only_priority=args.only_priority,
            )
        print("Safe artist import:")
        print("- before:")
        _print_coverage(result.before)
        for batch in result.batches:
            print(f"- batch {batch.batch}:")
            _print_process_result(batch.result, indent="  ")
        print("- totals:")
        _print_process_result(result.totals, indent="  ")
        print(f"- stopped_reason: {result.stopped_reason}")
        print("- after:")
        _print_coverage(result.after)
        return

    if args.command == "provider-test":
        with SessionLocal() as db:
            results = provider_test(args.artist, limit=args.limit, db=db)
        print(f"Provider results for {args.artist}: {len(results)}")
        for item in results[: args.limit]:
            print(
                f"- {item['title']} | {item['artist_name']} | "
                f"{item.get('duration_seconds') or 0}s | cover={'yes' if item.get('cover') else 'no'} | "
                f"match={item.get('artist_match_score')} | quality={item.get('quality_score')} | "
                f"decision={item.get('decision')}"
            )
        return

    if args.command == "retry-failed-artist-jobs":
        with SessionLocal() as db:
            result = retry_failed_artist_seed_jobs(db, limit=args.limit)
        print("Retry failed artist jobs:")
        print(f"- reset_jobs: {result['reset_jobs']}")
        return

    if args.command == "reset-import-job":
        with SessionLocal() as db:
            job = reset_import_job(db, args.job_id)
        if not job:
            print("Import job not found")
            raise SystemExit(1)
        print("Import job reset:")
        print(f"- id: {job.id}")
        print(f"- status: {job.status}")
        return

    if args.command == "import-coverage-summary":
        with SessionLocal() as db:
            summary = get_coverage_summary(db)
        print("Import coverage summary:")
        _print_coverage(summary)
        return

    if args.command == "export-import-report":
        with SessionLocal() as db:
            paths = export_import_report(db)
        _print_paths(paths)
        return

    if args.command == "export-artists-without-tracks":
        with SessionLocal() as db:
            paths = export_artists_without_tracks(db)
        _print_paths(paths)
        return

    if args.command == "export-tracks-needs-review":
        with SessionLocal() as db:
            paths = export_tracks_needs_review(db)
        _print_paths(paths)
        return


def _print_process_result(result, indent: str = "") -> None:
    for key in [
        "dry_run",
        "processed_jobs",
        "done_jobs",
        "failed_jobs",
        "fetched_count",
        "created_tracks",
        "linked_existing_tracks",
        "skipped_duplicates",
        "rejected_low_confidence",
        "rejected_low_quality",
        "marked_needs_review",
        "elapsed_seconds",
        "remaining_pending_jobs",
    ]:
        print(f"{indent}- {key}: {getattr(result, key)}")
    if result.errors:
        print(f"{indent}- errors:")
        for error in result.errors:
            print(f"{indent}  - {error}")


def _print_coverage(summary) -> None:
    for key, value in summary.model_dump().items():
        print(f"  - {key}: {value}")


def _print_paths(paths: dict[str, str]) -> None:
    print("Reports written:")
    for key, path in paths.items():
        print(f"- {key}: {path}")


if __name__ == "__main__":
    main()
