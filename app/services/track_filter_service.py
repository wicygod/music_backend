import re
from urllib.parse import urlparse

from app.models.track import Track
from app.services.artist_cleanup_service import artist_from_title, popular_track_key


MAX_FEED_DURATION_SECONDS = 15 * 60

BAD_CONTENT_RE = re.compile(
    r"\b("
    r"mr\s*beast|reaction|review|tutorial|podcast|interview|vlog|blog|lets\s*play|let'?s\s*play|"
    r"gameplay|walkthrough|live\s*stream|stream highlights|news|politics|celebrity|scandal|"
    r"home\s*loan|mortgage|eligibility|calculator|insurance|heart\s*rate|stethoscope|"
    r"throat|checkup|check\s*up|color\s*analysis|light\s*tracking|doctor|medical"
    r")\b",
    re.IGNORECASE,
)

TRUSTED_SOURCE_NAMES = {"soundcloud", "sc", "youtube", "youtube_music", "yt"}


def _source_host(source_url: str | None) -> str:
    if not source_url:
        return ""
    return (urlparse(str(source_url)).hostname or "").lower().removeprefix("www.")


def _known_music_provider(source_name: str | None, source_url: str | None) -> bool:
    name = (source_name or "").lower()
    host = _source_host(source_url)
    if name not in TRUSTED_SOURCE_NAMES:
        return False
    return (
        host == "soundcloud.com"
        or host.endswith(".soundcloud.com")
        or host in {"youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}
    )


def track_display_artist(track: Track) -> str:
    parsed = artist_from_title(track.title)
    if parsed:
        return parsed
    links = sorted(track.artist_links, key=lambda item: 0 if item.role == "main" else 1)
    for link in links:
        if link.artist and link.artist.name:
            return link.artist.name
    return ""


def is_music_track(track: Track) -> bool:
    if track.duration_seconds and track.duration_seconds > MAX_FEED_DURATION_SECONDS:
        return False
    haystack = " ".join(
        str(value or "")
        for value in (
            track.title,
            track.genre,
            track.tags_json,
            track.source_name,
            track.source_url,
            track_display_artist(track),
        )
    )
    if BAD_CONTENT_RE.search(haystack):
        return False
    if track.source_url and track.is_playable:
        return _known_music_provider(track.source_name, track.source_url)
    return True


def track_dedupe_key(track: Track) -> str:
    key = popular_track_key(track.title, track_display_artist(track))
    return key or f"id:{track.id}"


def dedupe_tracks(items: list[Track], limit: int | None = None, seen_keys: set[str] | None = None) -> list[Track]:
    seen = seen_keys if seen_keys is not None else set()
    result: list[Track] = []
    for track in items:
        if not is_music_track(track):
            continue
        key = track_dedupe_key(track)
        if key in seen:
            continue
        seen.add(key)
        result.append(track)
        if limit and len(result) >= limit:
            break
    return result
