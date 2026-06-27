import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.artist import Artist
from app.models.import_job import ImportJob
from app.repositories.artists import find_or_create_artist
from app.repositories.tracks import create_track_with_artist, find_duplicate_track
from app.schemas.import_job import ArtistSeedImportResult, ArtistSeedSummary, ImportJobCreate, SeedLoadResult
from app.schemas.track import TrackSeedCreate
from app.services.normalization_service import (
    clean_display_artist_name,
    detect_artist_region,
    normalize_artist_name,
    normalize_name,
    normalize_title,
)
from app.services.serialization_service import import_job_to_read, parse_json_object


DEMO_SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "demo_tracks.json"
ARTIST_SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "artist_seed.txt"

HIGH_PRIORITY_ARTISTS = {
    normalize_artist_name(name)
    for name in {
        "OG Buda",
        "MAYOT",
        "Soda Luv",
        "Big Baby Tape",
        "Kizaru",
        "MORGENSHTERN",
        "FACE",
        "PHARAOH",
        "Boulevard Depo",
        "GONE.Fludd",
        "Gone.Fludd",
        "LIZER",
        "OBLADAET",
        "Markul",
        "Feduk",
        "SALUKI",
        "Хаски",
        "Скриптонит",
        "ATL",
        "Noize MC",
        "Yanix",
        "Платина",
        "Пошлая Молли",
        "Дора",
        "Кишлак",
        "Три дня дождя",
        "Вышел покурить",
        "Мукка",
        "кино",
        "Джизус",
        "Хлеб",
        "Элджей",
        "Miyagi",
        "Andy Panda",
        "MACAN",
        "The Limba",
        "JONY",
        "Jah Khalib",
        "HammAli",
        "Navai",
        "Dabro",
        "Тима Белорусских",
        "Rauf & Faik",
        "shadowraze",
        "pyrokinesis",
        "BOOKER",
        "Baby Melo",
        "Bushido Zho",
        "Lovv66",
        "9mice",
        "Kai Angel",
        "ALBLAK 52",
        "Friendly Thug 52 NGG",
    }
}

NEEDS_REVIEW_ARTISTS = {
    normalize_artist_name(name)
    for name in {
        "Pure",
        "sly",
        "Otis",
        "Techno",
        "ЗИМА",
        "кино",
        "Паранойя",
        "Юпи",
        "свага",
        "CMH / Слава Марлоу",
    }
}


def create_import_job(db: Session, payload: ImportJobCreate) -> ImportJob:
    job = ImportJob(
        type=payload.type,
        payload_json=json.dumps(payload.payload),
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_import_job(db: Session, job_id: int) -> ImportJob | None:
    return db.get(ImportJob, job_id)


def read_import_job(db: Session, job_id: int):
    job = get_import_job(db, job_id)
    return import_job_to_read(job) if job else None


def load_demo_seed(db: Session, seed_path: Path = DEMO_SEED_PATH) -> SeedLoadResult:
    raw_items = json.loads(seed_path.read_text(encoding="utf-8"))
    created_tracks = 0
    created_artists = 0
    skipped_duplicates = 0

    for raw in raw_items:
        payload = _seed_item_to_payload(raw)
        normalized_artist = normalize_name(payload.artist)
        normalized_title = normalize_title(payload.title)

        duplicate = find_duplicate_track(
            db,
            normalized_artist=normalized_artist,
            normalized_title=normalized_title,
        )
        if duplicate:
            skipped_duplicates += 1
            continue

        artist, artist_created = find_or_create_artist(
            db,
            name=payload.artist,
            region=payload.artist_region,
            avatar_url=payload.artist_avatar_url,
            genres=payload.artist_genres,
            source_name=payload.source_name,
            source_external_id=f"artist:{normalized_artist}",
        )
        if artist_created:
            created_artists += 1

        create_track_with_artist(db, payload, artist)
        created_tracks += 1

    db.commit()
    return SeedLoadResult(
        created_tracks=created_tracks,
        skipped_duplicates=skipped_duplicates,
        created_artists=created_artists,
    )


def load_artist_seed(db: Session, seed_path: Path = ARTIST_SEED_PATH) -> ArtistSeedImportResult:
    raw_lines = seed_path.read_text(encoding="utf-8").splitlines()
    names = [clean_display_artist_name(line) for line in raw_lines if clean_display_artist_name(line)]
    total_lines = len(names)
    created_artists = 0
    skipped_duplicates = 0
    created_jobs = 0
    skipped_existing_jobs = 0
    needs_review_count = 0

    for name in names:
        normalized_name = normalize_artist_name(name)
        priority, tracks_target, needs_review = classify_seed_artist(name, normalized_name)
        region_hint = detect_artist_region(name)
        if needs_review:
            needs_review_count += 1

        artist, was_created = find_or_create_artist(
            db,
            name=name,
            region=region_hint,
            source_name="artist_seed",
            source_external_id=f"artist_seed:{normalized_name}",
            needs_review=needs_review,
            priority=priority,
            tracks_target=tracks_target,
            seed_source="artist_seed",
            import_status="needs_review" if needs_review else "pending",
        )
        if was_created:
            created_artists += 1
        else:
            skipped_duplicates += 1

        if has_artist_seed_job(db, artist.id):
            skipped_existing_jobs += 1
            continue

        db.add(
            ImportJob(
                type="artist_seed",
                payload_json=json.dumps(
                    {
                        "artist_id": artist.id,
                        "artist_name": artist.name,
                        "normalized_name": artist.normalized_name,
                        "region_hint": region_hint,
                        "priority": priority,
                        "tracks_target": tracks_target,
                    },
                    ensure_ascii=False,
                ),
                status="pending",
            )
        )
        created_jobs += 1

    db.commit()
    return ArtistSeedImportResult(
        total_lines=total_lines,
        created_artists=created_artists,
        skipped_duplicates=skipped_duplicates,
        created_jobs=created_jobs,
        skipped_existing_jobs=skipped_existing_jobs,
        needs_review=needs_review_count,
    )


def classify_seed_artist(name: str, normalized_name: str | None = None) -> tuple[str, int, bool]:
    normalized = normalized_name or normalize_artist_name(name)
    priority = "high" if normalized in HIGH_PRIORITY_ARTISTS else "normal"
    alnum_len = len("".join(char for char in normalized if char.isalnum()))
    needs_review = normalized in NEEDS_REVIEW_ARTISTS or (alnum_len <= 3 and priority != "high")
    tracks_target = 10 if needs_review else 60 if priority == "high" else 25
    return priority, tracks_target, needs_review


def has_artist_seed_job(db: Session, artist_id: int) -> bool:
    stmt = select(ImportJob).where(
        ImportJob.type == "artist_seed",
        ImportJob.status.in_(("pending", "running", "done")),
    )
    for job in db.execute(stmt).scalars():
        payload = parse_json_object(job.payload_json)
        if payload.get("artist_id") == artist_id:
            return True
    return False


def get_artist_seed_summary(db: Session) -> ArtistSeedSummary:
    total_artists = db.execute(select(func.count()).select_from(Artist)).scalar_one()
    high_priority = db.execute(select(func.count()).select_from(Artist).where(Artist.priority == "high")).scalar_one()
    normal_priority = db.execute(select(func.count()).select_from(Artist).where(Artist.priority == "normal")).scalar_one()
    low_priority = db.execute(select(func.count()).select_from(Artist).where(Artist.priority == "low")).scalar_one()
    needs_review = db.execute(select(func.count()).select_from(Artist).where(Artist.needs_review == True)).scalar_one()
    pending_import_jobs = db.execute(
        select(func.count()).select_from(ImportJob).where(
            ImportJob.type == "artist_seed",
            ImportJob.status == "pending",
        )
    ).scalar_one()
    return ArtistSeedSummary(
        total_artists=total_artists,
        high_priority=high_priority,
        normal_priority=normal_priority,
        low_priority=low_priority,
        needs_review=needs_review,
        pending_import_jobs=pending_import_jobs,
    )


def _seed_item_to_payload(raw: dict[str, Any]) -> TrackSeedCreate:
    return TrackSeedCreate(
        title=raw["title"],
        artist=raw["artist"],
        duration_seconds=raw["duration_seconds"],
        cover_url=raw["cover_url"],
        genre=raw.get("genre"),
        tags=raw.get("tags", []),
        language=raw.get("language"),
        region=raw.get("region", "unknown"),
        popularity_score=raw.get("popularity_score", 0.0),
        quality_score=raw.get("quality_score", 0.0),
        is_playable=False,
        audio_src=None,
        source_name=raw.get("source_name", "demo_seed"),
        source_external_id=raw.get("source_external_id"),
        source_url=raw.get("source_url"),
        artist_region=raw.get("artist_region", raw.get("region", "unknown")),
        artist_avatar_url=raw.get("artist_avatar_url"),
        artist_genres=raw.get("artist_genres", []),
        needs_review=raw.get("needs_review", False),
    )
