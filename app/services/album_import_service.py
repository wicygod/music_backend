from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import IMPORT_MAX_ALBUMS_PER_ARTIST, IMPORT_MAX_ALBUM_TRACKS_PER_ARTIST
from app.models.artist import Artist
from app.repositories.albums import link_album_track, upsert_album
from app.repositories.tracks import find_duplicate_track_for_artist, find_track_by_provider_external_id
from app.services.cover_service import extract_cover_url
from app.services.normalization_service import normalize_name
from app.services.popular_ranking_service import provider_popularity_score
from app.services.soundcloud_profile_service import canonical_soundcloud_profile_url


_HYDRATION_ASSIGNMENT_RE = re.compile(r"(?:window\.)?__sc_hydration\s*=", re.IGNORECASE)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_SOUNDCLOUD_PROVIDER = {
    "name": "soundcloud",
    "tag": "soundcloud",
    "default_genre": "soundcloud",
    "popularity_score": 75.0,
}
_album_hydration_lock = threading.Lock()
_hydrating_artist_ids: set[int] = set()
_hydrated_artist_ids: dict[int, float] = {}
_ALBUM_HYDRATION_COOLDOWN_SECONDS = 15 * 60


@dataclass(slots=True)
class AlbumImportStats:
    discovered_albums: int = 0
    imported_albums: int = 0
    created_albums: int = 0
    imported_tracks: int = 0
    linked_tracks: int = 0
    failed_albums: int = 0


def parse_soundcloud_album_hydration(
    html: str,
    *,
    expected_profile_url: str,
) -> dict | None:
    if not html:
        return None
    match = _HYDRATION_ASSIGNMENT_RE.search(html)
    if match is None:
        return None
    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    try:
        hydrated, _end = json.JSONDecoder().raw_decode(html, start)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(hydrated, list):
        return None

    expected_profile = canonical_soundcloud_profile_url(expected_profile_url)
    if expected_profile is None:
        return None
    for item in reversed(hydrated):
        if not isinstance(item, dict) or item.get("hydratable") != "playlist":
            continue
        payload = item.get("data")
        if not isinstance(payload, dict) or payload.get("kind") != "playlist":
            continue
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        if canonical_soundcloud_profile_url(user.get("permalink_url")) != expected_profile:
            continue
        permalink_url = str(payload.get("permalink_url") or "").strip()
        if canonical_soundcloud_profile_url(permalink_url) != expected_profile:
            continue
        if not payload.get("id") or not payload.get("title") or not isinstance(payload.get("tracks"), list):
            continue
        return payload
    return None


def discover_soundcloud_album_urls(
    profile_url: str,
    *,
    limit: int = IMPORT_MAX_ALBUMS_PER_ARTIST,
    timeout: float = 20.0,
) -> list[str]:
    canonical_profile = canonical_soundcloud_profile_url(profile_url)
    if canonical_profile is None:
        return []
    safe_limit = max(1, min(int(limit), 100))
    safe_timeout = max(2.0, min(float(timeout), 45.0))
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--flat-playlist",
        "--dump-single-json",
        "--playlist-end",
        str(safe_limit),
        "--socket-timeout",
        str(max(2, int(safe_timeout / 2))),
        "--retries",
        "1",
        "--extractor-retries",
        "1",
        f"{canonical_profile}/sets",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=safe_timeout,
            check=False,
        )
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else []
    result: list[str] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        url = str(entry.get("url") or "").strip() if isinstance(entry, dict) else ""
        if not url.startswith(f"{canonical_profile}/sets/") or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def fetch_soundcloud_album_payload(
    album_url: str,
    *,
    expected_profile_url: str,
    timeout: float = 10.0,
) -> dict | None:
    try:
        response = httpx.get(
            album_url,
            timeout=max(1.0, min(float(timeout), 30.0)),
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return parse_soundcloud_album_hydration(response.text, expected_profile_url=expected_profile_url)


def import_soundcloud_albums(
    db: Session,
    artist: Artist,
    *,
    max_albums: int = IMPORT_MAX_ALBUMS_PER_ARTIST,
    max_tracks: int = IMPORT_MAX_ALBUM_TRACKS_PER_ARTIST,
    dry_run: bool = False,
) -> AlbumImportStats:
    profile_url = canonical_soundcloud_profile_url(artist.source_url)
    if profile_url is None or (artist.source_name or "").lower() not in {"soundcloud", "sc"}:
        return AlbumImportStats()
    album_urls = discover_soundcloud_album_urls(profile_url, limit=max_albums)
    stats = AlbumImportStats(discovered_albums=len(album_urls))
    if not album_urls:
        return stats

    worker_count = min(4, len(album_urls))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="soundcloud-album") as pool:
        payloads = list(
            pool.map(
                lambda url: fetch_soundcloud_album_payload(url, expected_profile_url=profile_url),
                album_urls,
            )
        )

    remaining_tracks = max(1, min(int(max_tracks), 2000))
    for album_url, payload in zip(album_urls, payloads, strict=True):
        if not isinstance(payload, dict):
            stats.failed_albums += 1
            continue
        if not _is_release_payload(payload):
            # SoundCloud exposes albums and arbitrary user playlists through
            # the same /sets collection. The catalog must contain releases,
            # not listening playlists assembled by the account owner.
            continue
        tracks = [item for item in payload.get("tracks", []) if isinstance(item, dict)]
        if not tracks:
            stats.failed_albums += 1
            continue
        stats.imported_albums += 1
        if dry_run:
            stats.imported_tracks += min(len(tracks), remaining_tracks)
            remaining_tracks -= min(len(tracks), remaining_tracks)
            if remaining_tracks <= 0:
                break
            continue

        release_date = _parse_soundcloud_datetime(payload.get("release_date") or payload.get("published_at"))
        album_score, _reliable = provider_popularity_score(
            like_count=payload.get("likes_count"),
            repost_count=payload.get("reposts_count"),
            timestamp=release_date.timestamp() if release_date else None,
            fallback=50.0,
        )
        album, created = upsert_album(
            db,
            artist=artist,
            title=str(payload.get("title") or "Untitled album"),
            album_type=_album_type(payload),
            cover_url=extract_cover_url(payload, provider_name="soundcloud"),
            release_date=release_date,
            track_count=len(tracks),
            source_name="soundcloud",
            source_external_id=str(payload["id"]),
            source_url=album_url,
            popularity_score=album_score,
        )
        stats.created_albums += int(created)
        db.commit()
        db.refresh(album)

        for position, track_payload in enumerate(tracks, start=1):
            if remaining_tracks <= 0:
                break
            if not _is_available_track(track_payload):
                continue
            remaining_tracks -= 1
            stats.imported_tracks += 1
            provider_entry = _provider_entry(track_payload, artist, album)
            try:
                # Imported locally to avoid a module cycle: search hydration
                # and artist jobs both reuse the same provider-track upsert.
                from app.services.search_service import _save_provider_entry

                _save_provider_entry(db, artist.name, _SOUNDCLOUD_PROVIDER, provider_entry)
            except Exception:
                db.rollback()
                album = db.get(type(album), album.id)
                if album is None:
                    break
                continue
            track = find_track_by_provider_external_id(
                db,
                provider="soundcloud",
                external_id=str(track_payload.get("id")),
            )
            if track is None:
                duration = round(max(0, int(track_payload.get("full_duration") or track_payload.get("duration") or 0)) / 1000)
                track = find_duplicate_track_for_artist(
                    db,
                    normalized_artist=normalize_name(artist.name),
                    title=str(track_payload.get("title") or ""),
                    duration_seconds=duration,
                )
            if track is None:
                continue
            link_album_track(
                db,
                album_id=album.id,
                track_id=track.id,
                disc_number=1,
                track_number=position,
            )
            stats.linked_tracks += 1
        album.track_count = stats_for_album = len(album.track_links) if album.track_links else len(tracks)
        if stats_for_album <= 0:
            album.track_count = len(tracks)
        db.add(album)
        db.commit()
        if remaining_tracks <= 0:
            break
    return stats


def import_soundcloud_albums_for_artist(artist_id: int) -> AlbumImportStats:
    from app.database import SessionLocal

    normalized_artist_id = int(artist_id)
    try:
        with SessionLocal() as db:
            artist = db.get(Artist, normalized_artist_id)
            if artist is None:
                return AlbumImportStats()
            try:
                return import_soundcloud_albums(db, artist)
            except Exception:
                db.rollback()
                return AlbumImportStats(failed_albums=1)
    finally:
        with _album_hydration_lock:
            _hydrating_artist_ids.discard(normalized_artist_id)
            _hydrated_artist_ids[normalized_artist_id] = time.monotonic()


def schedule_artist_album_hydration(artist_id: int, background_tasks) -> bool:
    normalized_artist_id = int(artist_id)
    with _album_hydration_lock:
        if normalized_artist_id in _hydrating_artist_ids:
            return False
        hydrated_at = _hydrated_artist_ids.get(normalized_artist_id)
        if hydrated_at and time.monotonic() - hydrated_at < _ALBUM_HYDRATION_COOLDOWN_SECONDS:
            return False
        _hydrating_artist_ids.add(normalized_artist_id)
    try:
        background_tasks.add_task(import_soundcloud_albums_for_artist, normalized_artist_id)
    except Exception:
        with _album_hydration_lock:
            _hydrating_artist_ids.discard(normalized_artist_id)
        raise
    return True


def album_hydration_pending(artist_ids: list[int] | None = None) -> bool:
    with _album_hydration_lock:
        if artist_ids is None:
            return bool(_hydrating_artist_ids)
        return any(int(artist_id) in _hydrating_artist_ids for artist_id in artist_ids)


def _provider_entry(track: dict, artist: Artist, album) -> dict:
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    duration_ms = max(0, int(track.get("full_duration") or track.get("duration") or 0))
    return {
        "id": str(track.get("id")),
        "title": str(track.get("title") or "").strip(),
        "uploader": str(user.get("username") or artist.name),
        "artist": str((track.get("publisher_metadata") or {}).get("artist") or artist.name),
        "uploader_url": str(user.get("permalink_url") or artist.source_url or ""),
        "webpage_url": str(track.get("permalink_url") or ""),
        "duration": round(duration_ms / 1000),
        "artwork_url": track.get("artwork_url") or album.cover_url,
        "genre": track.get("genre") or None,
        "view_count": track.get("playback_count"),
        "like_count": track.get("likes_count"),
        "repost_count": track.get("reposts_count"),
        "timestamp": _timestamp(track.get("release_date") or track.get("display_date")),
        "album": album.title,
        "album_id": album.source_external_id,
    }


def _is_available_track(track: dict) -> bool:
    return bool(
        track.get("id")
        and track.get("title")
        and track.get("permalink_url")
        and track.get("public", True)
        and track.get("state", "finished") == "finished"
        and track.get("streamable", True)
    )


def _album_type(payload: dict) -> str:
    raw = str(payload.get("set_type") or "").strip().lower()
    if raw in {"album", "ep", "single", "compilation"}:
        return raw
    if payload.get("is_album"):
        return "album"
    return "playlist"


def _is_release_payload(payload: dict) -> bool:
    return _album_type(payload) in {"album", "ep", "single", "compilation"}


def _parse_soundcloud_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _timestamp(value: object) -> float | None:
    parsed = _parse_soundcloud_datetime(value)
    return parsed.timestamp() if parsed else None
