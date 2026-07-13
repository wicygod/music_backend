# Million Dollars Music Backend

Authenticated catalog and playback backend for the Million Dollars Music app.
It stores catalog and per-account data, resolves approved music providers, and
serves ticketed seekable audio through a bounded local cache.

## Install

```powershell
cd D:\million_dollars_VIBECODE\music_backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/VPS:

```bash
cd /path/to/music_backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set unique values for `MUSIC_APP_AUTH_TOKEN`, `MUSIC_ADMIN_API_KEY`, and
`MUSIC_JWT_SECRET` in the service environment before starting the API. Import
endpoints require the admin key in `X-Admin-Key`; normal app endpoints require
the app token and a signed account token.

## Database

SQLite is used by default:

```powershell
alembic upgrade head
```

The default database file is `music_catalog.db` in the repository root. This
repository currently includes a test SQLite catalog so the API has data right
after clone. To use a different database later, set `MUSIC_DATABASE_URL`, for
example:

```powershell
$env:MUSIC_DATABASE_URL = "sqlite:///./music_catalog.db"
alembic upgrade head
```

PostgreSQL is supported through psycopg. Create the database, set the URL, and
run the same migrations before starting the API:

```bash
export MUSIC_DATABASE_URL="postgresql+psycopg://music:password@127.0.0.1:5432/music"
alembic upgrade head
```

Production startup runs `alembic upgrade head` through the systemd unit before
Uvicorn. `init_db()` still creates an empty schema for isolated development and
test databases, but all changes to an existing schema belong in `migrations/`.

## Demo Seed

Load the demo metadata-only catalog:

```powershell
python -m app.cli seed-demo
```

The loader is idempotent for the demo data. It skips duplicates using
`normalized_artist + normalized_title`.

You can also load it through the API:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/import/demo-seed
```

## Artist Seed

Load the raw artist seed list and create future import jobs:

```powershell
python -m app.cli seed-artists
```

This step only creates artist metadata and `artist_seed` import jobs. It does
not search real tracks, connect to external providers, download audio, or create
streaming URLs.

The loader is idempotent:

- artists are deduplicated by `normalized_name`;
- repeated runs do not create duplicate `artist_seed` jobs for the same artist;
- region is only a hint: `ru` for Cyrillic names, `global` for Latin names,
  otherwise `unknown`;
- large artists get `priority=high` and a larger `tracks_target`;
- ambiguous names are marked with `needs_review=true`.

API alternatives:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/import/seed-artists
Invoke-RestMethod http://127.0.0.1:8000/api/import/seed-artists/summary
Invoke-RestMethod "http://127.0.0.1:8000/api/artists?priority=high&limit=20"
```

## Metadata Providers

The first metadata provider uses the iTunes Search API. It is used only for
metadata discovery:

- no audio is downloaded;
- no streams are created;
- provider `previewUrl` is never saved as `audio_src`;
- imported tracks are stored with `is_playable=false` and `audio_src=null`.

Test the provider without saving anything:

```powershell
python -m app.cli provider-test "Miyagi"
python -m app.cli provider-test "Mariah Carey"
```

Process a small batch of pending artist jobs:

```powershell
python -m app.cli process-artist-jobs --limit 5
```

Run the same batch safely without writing tracks or changing job status:

```powershell
python -m app.cli process-artist-jobs --limit 5 --dry-run
```

Process all pending artist jobs with a small pause between batches:

```powershell
python -m app.cli process-all-artist-jobs
```

Retry or reset failed jobs:

```powershell
python -m app.cli retry-failed-artist-jobs --limit 10
python -m app.cli reset-import-job 123
```

Inspect catalog coverage:

```powershell
python -m app.cli import-coverage-summary
```

Run a controlled import with per-batch summaries:

```powershell
python -m app.cli safe-import-artists --batch-size 10 --max-batches 2 --dry-run
python -m app.cli safe-import-artists --batch-size 10 --max-batches 5
```

Optional filters and safety switch:

```powershell
python -m app.cli safe-import-artists --batch-size 10 --max-batches 3 --only-priority high
python -m app.cli safe-import-artists --batch-size 10 --max-batches 5 --stop-on-high-failure-rate
```

Export text reports into `reports/`:

```powershell
python -m app.cli export-import-report
python -m app.cli export-artists-without-tracks
python -m app.cli export-tracks-needs-review
```

Before adding more providers, check:

- `artists_with_zero_tracks` in coverage summary;
- `tracks_needs_review` as a share of total tracks;
- `import_job_reports.json` for high low-confidence or failure rates;
- `artists_without_tracks.json` to decide which providers are missing coverage.

Import quality rules:

- `artist_match_score >= 0.75`: save as a normal candidate;
- `0.55 <= artist_match_score < 0.75`: save with `needs_review=true`;
- stricter artists marked `needs_review=true` need stronger matches;
- `quality_score >= 70`: normal card;
- `45 <= quality_score < 70`: save with `needs_review=true`;
- `quality_score < 45`: reject from the catalog.

Import reports are stored in `import_job_reports` with fetched, saved,
duplicate, low-confidence, low-quality, and review counts.

API alternatives:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/import/provider-test?artist=Miyagi"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/import/process-artist-jobs?limit=5&dry_run=true"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/import/process-artist-jobs?limit=5"
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/import/safe-import-artists?batch_size=10&max_batches=2&dry_run=true"
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/import/process-all-artist-jobs
Invoke-RestMethod http://127.0.0.1:8000/api/import/jobs/summary
Invoke-RestMethod http://127.0.0.1:8000/api/import/coverage-summary
Invoke-RestMethod http://127.0.0.1:8000/api/import/artists-without-tracks
Invoke-RestMethod http://127.0.0.1:8000/api/feed/home
```

## Run

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

For a VPS process manager, use the same app import path without reload:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Expose production traffic only through an HTTPS reverse proxy; never publish
Uvicorn port 8000 directly.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

The API returns a `Server-Timing` header for registration, search, stream
ticket/setup, and listening-progress requests. Aggregated counts, failures,
average, p50, and p95 latency are available to administrators:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/admin/metrics -Headers @{ "X-Admin-Key" = $env:MUSIC_ADMIN_API_KEY; "X-App-Token" = $env:MUSIC_APP_AUTH_TOKEN }
```

## Endpoints

- `GET /api/health`
- `GET /api/feed/home`
- `GET /api/search?q=...`
- `GET /api/artists?q=&region=&priority=&needs_review=&limit=&offset=`
- `GET /api/tracks/{track_id}`
- `GET /api/artists/{artist_id}`
- `GET /api/artists/{artist_id}/tracks`
- `POST /api/user/playlists`
- `GET /api/user/playlists`
- `POST /api/user/playlists/{playlist_id}/tracks/{track_id}`
- `DELETE /api/user/playlists/{playlist_id}/tracks/{track_id}`
- `POST /api/user/favorites/{track_id}`
- `DELETE /api/user/favorites/{track_id}`
- `POST /api/import/jobs`
- `GET /api/import/jobs/{job_id}`
- `GET /api/import/jobs/summary`
- `POST /api/import/demo-seed`
- `POST /api/import/seed-artists`
- `GET /api/import/seed-artists/summary`
- `POST /api/import/process-artist-jobs?limit=5`
- `POST /api/import/process-artist-jobs?limit=5&dry_run=true`
- `POST /api/import/process-all-artist-jobs`
- `POST /api/import/safe-import-artists?batch_size=&max_batches=&dry_run=&stop_on_high_failure_rate=&only_priority=`
- `GET /api/import/provider-test?artist=...`
- `POST /api/import/retry-failed-artist-jobs?limit=10`
- `POST /api/import/jobs/{job_id}/reset`
- `GET /api/import/coverage-summary`
- `GET /api/import/artists-without-tracks?limit=&offset=&priority=&needs_review=`

## Frontend Integration Later

The Tauri frontend can later replace its local metadata provider with:

```ts
const response = await fetch("http://127.0.0.1:8000/api/feed/home");
const feed = await response.json();
```

All current demo tracks are metadata-only:

```json
{
  "is_playable": false,
  "audio_src": null
}
```

The frontend should keep the existing guard that prevents playback when
`audio_src` is missing.

## Next Step

The next backend stage is to implement metadata providers that can process
pending `artist_seed` jobs and discover metadata-only track cards for each
artist. Those future tracks should still use:

```json
{
  "is_playable": false,
  "audio_src": null
}
```
