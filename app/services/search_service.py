import hashlib
import json
import random
import re
import time
import threading
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from fastapi import BackgroundTasks
import yt_dlp
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.repositories.artists import find_or_create_artist
from app.repositories.tracks import search_tracks
from app.repositories.tracks import (
    create_track_with_artist,
    ensure_track_artist_link,
    find_duplicate_track_for_artist,
    find_track_by_provider_external_id,
    search_track_fuzzy_candidates,
)
from app.schemas.track import TrackRead
from app.schemas.track import TrackSeedCreate
from app.models.track import TrackArtist
from app.services.artist_cleanup_service import (
    artist_from_title,
    clean_provider_artist,
    provider_authority_score,
    source_profile_matches_artist,
    title_without_artist_prefix,
)
from app.services.cover_service import extract_cover_url, fetch_soundcloud_oembed_cover
from app.services.normalization_service import (
    all_search_tokens_match,
    clean_display_artist_name,
    compact_search_text,
    detect_artist_region,
    normalize_name,
    normalize_search_text,
    normalize_title,
    search_token_matches,
    search_tokens,
)
from app.services.popular_ranking_service import PROVIDER_POPULARITY_TAG, provider_popularity_score
from app.services.serialization_service import track_to_read
from app.services.soundcloud_profile_service import fetch_soundcloud_profile
from app.services.proxy_rotator import proxy_rotator
from app.services.track_filter_service import is_music_track


SEARCH_RESULT_LIMIT = 150
EXTERNAL_PARSE_LIMIT = 50
HYDRATION_COOLDOWN_SECONDS = 2 * 60
VARIANT_QUOTA = 2
DEDUP_SIMILARITY_THRESHOLD = 0.88
DEDUP_CATEGORY_QUOTAS = {
    "original": 1,
    "speed": 1,
    "slowed": 1,
    "custom": 1,
}
MIN_MUSIC_DURATION_SECONDS = 10
MAX_MUSIC_DURATION_SECONDS = 15 * 60
COOKIES_FILE = Path("secrets/cookies.txt")
_hydration_lock = threading.Lock()
_hydrating_queries: set[str] = set()
_scheduled_queries: set[str] = set()
_hydrated_queries: dict[str, float] = {}
_hydration_slots = threading.BoundedSemaphore(2)

VARIANT_PATTERNS = {
    "slowed": re.compile(r"\bslowed\b|\bslow\s*\+\s*reverb\b|\bslowed\s*\+\s*reverb\b", re.IGNORECASE),
    "reverb": re.compile(r"\breverb\b", re.IGNORECASE),
    "speed": re.compile(r"\bspeed\s*up\b|\bspeedup\b|\bsped\s*up\b|\bspedup\b|\bnightcore\b", re.IGNORECASE),
}
CUSTOM_VARIANT_RE = re.compile(
    r"\b("
    r"breakcore|bass\s*boost(?:ed)?|instrumental|karaoke|remix|deep\s*remix|edit|bootleg|mashup|"
    r"cover|ai|8d|lofi|acoustic|live|version|visualizer|sped\s*down|pitch(?:ed)?"
    r"|кавер|ремикс|инструментал"
    r")\b",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s]*)?", re.IGNORECASE)
FILE_EXT_RE = re.compile(r"\.(?:mp3|m4a|webm|wav|flac|aac|ogg)\b", re.IGNORECASE)
BRACKET_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
NON_TITLE_CHARS_RE = re.compile(r"[^\w\s]+", re.UNICODE)
SPACES_RE = re.compile(r"\s+")
BAD_VIDEO_TERMS_RE = re.compile(
    r"\b("
    r"reaction|review|tutorial|podcast|interview|vlog|blog|lets\s*play|let'?s\s*play|gameplay|"
    r"walkthrough|stream|live\s*stream|news|politics|mock(?:s|ed|ing)?|blast(?:s|ed|ing)?|"
    r"humiliation|hollywood|grammys|ai\s+music\s+video|ai\s+cover|"
    r"relationship|robbed|bizarre|insecurity|celebrity|scandal|"
    r"обзор|реакц(?:ия|ии)|прохожд(?:ение|ения)|летсплей|стрим"
    r")\b",
    re.IGNORECASE,
)
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)


def _log(message: str) -> None:
    print(message, flush=True)


def _query_log_key(query: str) -> str:
    return hashlib.sha256(normalize_search_text(query).encode("utf-8")).hexdigest()[:12]


def _yt_dlp_options(**overrides) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 10,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "http_headers": headers,
    }
    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        options["cookiefile"] = str(COOKIES_FILE)
    proxy = proxy_rotator.next_proxy()
    if proxy:
        options["proxy"] = proxy
    options.update(overrides)
    return options


def _source_host(source_url: str | None) -> str:
    if not source_url:
        return ""
    return (urlparse(str(source_url)).hostname or "").lower().removeprefix("www.")


def _is_soundcloud_source(source_url: str | None) -> bool:
    host = _source_host(source_url)
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def _youtube_video_id(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = urlparse(str(source_url))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in {"youtube.com", "music.youtube.com", "m.youtube.com"}:
        return (parse_qs(parsed.query).get("v") or [None])[0]
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None
    return None


def _canonical_youtube_music_url(source_url: str | None, external_id: str | None = None) -> str | None:
    video_id = _youtube_video_id(source_url) or (external_id if external_id and not external_id.isdigit() else None)
    if not video_id:
        return None
    return f"https://music.youtube.com/watch?v={video_id}"


def _is_youtube_music_source(source_url: str | None) -> bool:
    return _youtube_video_id(source_url) is not None


def _candidate_source_url(provider_name: str, entry: dict) -> str | None:
    source_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if provider_name == "youtube":
        external_id = str(entry.get("id") or "").strip()
        if source_url and not _youtube_video_id(str(source_url)) and not external_id:
            return None
        return _canonical_youtube_music_url(str(source_url or ""), external_id)
    if provider_name == "soundcloud" and _is_soundcloud_source(source_url):
        return str(source_url)
    return None


def _is_allowed_provider_entry(provider_name: str, entry: dict) -> bool:
    source_url = _candidate_source_url(provider_name, entry)
    if not source_url:
        return False

    duration_seconds = _safe_int(entry.get("duration"))
    if (
        duration_seconds < MIN_MUSIC_DURATION_SECONDS
        or duration_seconds > MAX_MUSIC_DURATION_SECONDS
    ):
        return False

    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "creator", "description", "webpage_url", "url")
    )
    if BAD_VIDEO_TERMS_RE.search(haystack):
        return False

    if provider_name == "soundcloud":
        return _is_soundcloud_source(source_url)

    if provider_name == "youtube":
        categories = " ".join(str(item) for item in (entry.get("categories") or []))
        channel = str(entry.get("channel") or entry.get("uploader") or "")
        title = str(entry.get("title") or "")
        return (
            _is_youtube_music_source(source_url)
            and (
                "music" in categories.lower()
                or "music.youtube.com" in str(entry.get("webpage_url") or entry.get("url") or "").lower()
                or channel.lower().endswith(" - topic")
                or "official audio" in title.lower()
                or "official music video" in title.lower()
            )
        )

    return False

SEARCH_PROVIDERS = (
    {
        "name": "soundcloud",
        "search_prefix": "scsearch",
        "tag": "soundcloud",
        "default_genre": "soundcloud",
        "popularity_score": 75.0,
    },
    {
        "name": "youtube",
        "search_prefix": None,
        "search_url": "https://music.youtube.com/search?q={query}",
        "tag": "youtube_music",
        "default_genre": "youtube_music",
        "popularity_score": 65.0,
    },
)


def _playable_provider_count(results: list) -> int:
    return sum(
        bool(track.is_playable)
        and _is_known_provider_source(track.source_name, track.source_url)
        for track in results
    )


def _variant_types(text: str | None) -> tuple[str, ...]:
    haystack = (text or "").lower()
    return tuple(name for name, pattern in VARIANT_PATTERNS.items() if pattern.search(haystack))


def _dedupe_category(text: str | None) -> str:
    haystack = (text or "").lower()
    variant_types = set(_variant_types(haystack))
    if variant_types & {"speed"}:
        return "speed"
    if variant_types & {"slowed", "reverb"}:
        return "slowed"
    if CUSTOM_VARIANT_RE.search(haystack):
        return "custom"
    return "original"


def _clean_title_for_grouping(title: str | None) -> str:
    cleaned = title_without_artist_prefix(title).lower()
    cleaned = DOMAIN_RE.sub(" ", cleaned)
    cleaned = FILE_EXT_RE.sub(" ", cleaned)
    cleaned = BRACKET_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:official\s+)?(?:audio|video|music\s+video|lyrics?|lyric\s+video)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:speed\s*up|speedup|sped\s*up|spedup|nightcore|slowed|reverb|slow\s*\+\s*reverb)\b", " ", cleaned)
    cleaned = CUSTOM_VARIANT_RE.sub(" ", cleaned)
    cleaned = NON_TITLE_CHARS_RE.sub(" ", cleaned)
    cleaned = SPACES_RE.sub(" ", cleaned).strip()
    return cleaned or "unknown"


def _title_from_item(item) -> str:
    if isinstance(item, dict):
        return str(item.get("title") or "")
    return str(getattr(item, "title", "") or "")


def _title_tokens(value: str) -> set[str]:
    return {token for token in value.split() if len(token) >= 3}


def _looks_like_same_song(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 3 and (left in right or right in left):
        return True
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if left_tokens and right_tokens:
        if left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens):
            return True
        jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if jaccard >= 0.85:
            return True
    return SequenceMatcher(None, left, right).ratio() >= DEDUP_SIMILARITY_THRESHOLD


def _title_spam_penalty(title: str) -> int:
    haystack = (title or "").lower()
    penalty = 0
    if DOMAIN_RE.search(haystack):
        penalty += 2
    if FILE_EXT_RE.search(haystack):
        penalty += 2
    if "spotidown" in haystack or "lightaudio" in haystack:
        penalty += 3
    return penalty


def _empty_dedupe_counts() -> dict[str, int]:
    return {name: 0 for name in DEDUP_CATEGORY_QUOTAS}


def _take_with_song_dedupe(groups: list[dict], item) -> bool:
    title = _title_from_item(item)
    base_title = _clean_title_for_grouping(title)
    parsed_artist = artist_from_title(title)
    artist_key = normalize_name(parsed_artist or _item_artist_text(item))
    category = _dedupe_category(title)
    group = next(
        (
            candidate
            for candidate in groups
            if _looks_like_same_song(base_title, candidate["base"])
            and (
                not artist_key
                or not candidate["artist"]
                or artist_key == candidate["artist"]
            )
        ),
        None,
    )
    if not group:
        group = {"base": base_title, "artist": artist_key, "counts": _empty_dedupe_counts()}
        groups.append(group)
    if group["counts"].get(category, 0) >= DEDUP_CATEGORY_QUOTAS[category]:
        return False
    group["counts"][category] = group["counts"].get(category, 0) + 1
    return True


def _dedupe_groups_from_items(items: list) -> list[dict]:
    groups: list[dict] = []
    for item in items:
        _take_with_song_dedupe(groups, item)
    return groups


def _apply_song_dedupe_quota(items: list, limit: int = SEARCH_RESULT_LIMIT) -> list:
    groups: list[dict] = []
    filtered = []
    ordered_items = sorted(enumerate(items), key=lambda pair: (_title_spam_penalty(_title_from_item(pair[1])), pair[0]))
    for _index, item in ordered_items:
        if not _take_with_song_dedupe(groups, item):
            continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


def _variant_counter_from_tracks(results: list) -> dict[str, int]:
    counter = {name: 0 for name in VARIANT_PATTERNS}
    counter["_total"] = 0
    for track in results:
        variant_types = _variant_types(track.title)
        if not variant_types:
            continue
        counter["_total"] += 1
        for variant_type in variant_types:
            counter[variant_type] += 1
    return counter


def _can_take_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> bool:
    return not variant_types or counter.get("_total", 0) < VARIANT_QUOTA


def _count_variant(counter: dict[str, int], variant_types: tuple[str, ...]) -> None:
    if not variant_types:
        return
    counter["_total"] = counter.get("_total", 0) + 1
    for variant_type in variant_types:
        counter[variant_type] = counter.get(variant_type, 0) + 1


def _apply_variant_quota(results: list, limit: int = SEARCH_RESULT_LIMIT) -> list:
    return _apply_song_dedupe_quota(results, limit=limit)


def _is_clean_catalog_track(track) -> bool:
    if not is_music_track(track):
        return False
    if BAD_VIDEO_TERMS_RE.search(" ".join(str(value or "") for value in (track.title, track.source_url))):
        return False
    if not bool(getattr(track, "is_playable", False)):
        return False
    if getattr(track, "audio_src", None):
        return True
    if not track.source_url or _safe_int(getattr(track, "duration_seconds", 0)) < MIN_MUSIC_DURATION_SECONDS:
        return False
    return _is_known_provider_source(track.source_name, track.source_url)


def _catalog_track_matches_query(track, query: str) -> bool:
    tokens = _query_tokens(query)
    artists = _item_artist_text(track)
    title_haystack = normalize_search_text(track.title)
    artist_haystack = normalize_search_text(artists)
    genre_haystack = normalize_search_text(str(getattr(track, "genre", "") or ""))
    compact_query = compact_search_text(query)
    if len(compact_query) >= 3 and compact_query in compact_search_text(f"{artists} {track.title}"):
        return True
    if not tokens:
        return False
    if _all_tokens_match(tokens, title_haystack):
        return True
    if _all_tokens_match(tokens, artist_haystack):
        return True
    return _all_tokens_match(
        tokens,
        f"{artist_haystack} {title_haystack} {genre_haystack}",
    )


def _query_tokens(query: str) -> list[str]:
    return search_tokens(query)


def _token_matches_text(token: str, normalized_text: str) -> bool:
    return search_token_matches(token, normalized_text)


def _all_tokens_match(tokens: list[str], normalized_text: str) -> bool:
    return all_search_tokens_match(tokens, normalized_text)


def _title_matches_query(title: str | None, query: str) -> bool:
    tokens = _query_tokens(query)
    if len(tokens) <= 1:
        return True
    haystack = normalize_name(title or "")
    return _all_tokens_match(tokens, haystack)


def _item_artist_text(item) -> str:
    """Extract artist name from a Track object or a provider dict."""
    if isinstance(item, dict):
        return _entry_artist_name(item, "")
    direct = getattr(item, "artist", None)
    if isinstance(direct, str):
        return direct
    links = list(getattr(item, "artist_links", []) or [])
    main_links = [link for link in links if getattr(link, "role", "main") == "main"]
    source_owner_links = [
        link
        for link in links
        if getattr(link, "role", "main") == "uploader"
        and getattr(link, "artist", None)
        and source_profile_matches_artist(
            getattr(item, "source_url", None),
            getattr(link.artist, "name", None),
        )
    ]
    relevant_links = [*main_links, *source_owner_links] or links
    names: list[str] = []
    seen: set[str] = set()
    for link in relevant_links:
        artist = getattr(link, "artist", None)
        name = str(getattr(artist, "name", "") or "").strip()
        normalized = normalize_name(name)
        if not name or normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return " ".join(names)


def _item_title_text(item) -> str:
    """Extract raw title from a Track object or a provider dict."""
    return _title_from_item(item)


def _relevance_score(query: str, title_raw: str, artist_raw: str) -> int:
    """Return a relevance tier for *query* against a title/artist pair.

    Lower values mean higher relevance (used as a sort key).

    Tiers
    -----
    0 – exact match of normalised title to query
    1 – exact match after stripping "Artist - " prefix from the raw title
    2 – exact match of normalised artist to query
    3 – all query tokens found inside the title
    4 – all query tokens found inside the artist
    5 – all query tokens found across artist + title combined
    6 – no match
    """
    norm_title_query = normalize_search_text(query)
    norm_artist_query = normalize_name(query)
    compact_query = compact_search_text(query)
    tokens = _query_tokens(query)
    norm_title = normalize_search_text(title_raw)
    norm_artist = normalize_name(artist_raw)

    # Tier 0: exact title match
    if norm_title == norm_title_query or (
        len(compact_query) >= 3
        and compact_search_text(title_raw) == compact_query
    ):
        return 0

    # Tier 1: exact title match after stripping "Artist - " prefix
    stripped = normalize_search_text(title_without_artist_prefix(title_raw))
    if stripped != norm_title and (
        stripped == norm_title_query
        or (
            len(compact_query) >= 3
            and compact_search_text(stripped) == compact_query
        )
    ):
        return 1

    # Tier 2: exact artist match
    if norm_artist == norm_artist_query or (
        len(compact_query) >= 3
        and compact_search_text(artist_raw) == compact_query
    ):
        return 2

    # Tier 3: all tokens match inside title
    if _all_tokens_match(tokens, norm_title):
        return 3

    # Tier 4: all tokens match inside artist
    if _all_tokens_match(tokens, norm_artist):
        return 4

    # Tier 5: all tokens match across artist + title
    if _all_tokens_match(tokens, f"{norm_artist} {norm_title}"):
        return 5

    return 6


def _prefer_title_matches(items: list, query: str) -> list:
    if not items:
        return items

    return [
        item
        for _index, item in sorted(
            enumerate(items),
            key=lambda pair: (
                _relevance_bucket(
                    _relevance_score(query, _item_title_text(pair[1]), _item_artist_text(pair[1]))
                ),
                *_originality_sort_key(pair[1], query),
                pair[0],
            ),
        )
    ]


def _relevance_bucket(tier: int) -> int:
    # Provider titles commonly include the artist prefix. Treat
    # ``Artist - Song`` and ``Song`` as the same textual match so authority,
    # completeness and popularity choose the original instead of a fake
    # title-shaped uploader.
    return 0 if tier <= 1 else tier


def _originality_sort_key(item, query: str) -> tuple:
    """Prefer an authoritative playable original inside the same relevance tier.

    Search providers frequently return title-shaped uploader accounts and old
    zero-duration placeholders. Exact text still wins first, but those records
    must not outrank a complete track from the artist's canonical profile.
    """

    title = normalize_search_text(_item_title_text(item))
    artist = normalize_name(_item_artist_text(item))
    normalized_title_query = normalize_search_text(query)
    normalized_artist_query = normalize_name(query)
    variant_rank = 0 if _dedupe_category(_item_title_text(item)) == "original" else 1
    if isinstance(item, dict):
        duration = _safe_int(item.get("duration"))
        source_url = str(item.get("webpage_url") or item.get("url") or "")
        playable = bool(source_url)
        quality = 100.0 if playable and duration >= 30 else 50.0 if playable else 0.0
        popularity = _safe_float(
            item.get("view_count")
            or item.get("playback_count")
            or item.get("like_count")
        )
        uploader_url = str(item.get("uploader_url") or item.get("channel_url") or "")
        parsed_artist = artist_from_title(_item_title_text(item))
        credited_artist = parsed_artist or _entry_artist_name(item, "")
        source_owner_match = int(source_profile_matches_artist(uploader_url, credited_artist))
        canonical = source_owner_match
        verified = int(bool(item.get("uploader_verified") or item.get("channel_is_verified") or item.get("verified")))
        followers = _safe_int(item.get("uploader_follower_count") or item.get("channel_follower_count"))
        metadata_noise = 0
        needs_review = 0
    else:
        duration = _safe_int(getattr(item, "duration_seconds", 0))
        source_url = str(getattr(item, "source_url", "") or "")
        playable = bool(getattr(item, "is_playable", False) and _is_known_provider_source(getattr(item, "source_name", None), source_url))
        quality = _safe_float(getattr(item, "quality_score", 0.0))
        popularity = _safe_float(getattr(item, "popularity_score", 0.0))
        links = list(getattr(item, "artist_links", []) or [])
        main_links = [link for link in links if getattr(link, "role", "main") == "main"]
        primary_links = main_links or links
        primary_artists = [
            link.artist
            for link in primary_links
            if getattr(link, "artist", None) is not None
        ]
        canonical = int(any(bool(getattr(linked, "is_canonical", False)) for linked in primary_artists))
        verified = int(any(bool(getattr(linked, "source_verified", False)) for linked in primary_artists))
        followers = max((int(getattr(linked, "source_followers_count", 0) or 0) for linked in primary_artists), default=0)
        source_owner_match = int(any(
            source_profile_matches_artist(source_url, getattr(link.artist, "name", None))
            for link in links
            if getattr(link, "artist", None) is not None
        ))
        metadata_noise = max(0, sum(1 for link in links if getattr(link, "role", "main") == "uploader") - 1)
        needs_review = int(bool(getattr(item, "needs_review", False)))
    authoritative_identity = bool(canonical or verified or source_owner_match or followers > 0)
    suspicious_identity = int(
        bool(normalized_title_query)
        and title == normalized_title_query
        and artist == normalized_artist_query
        and not authoritative_identity
    )
    duration_rank = 0 if duration >= 30 else 1 if duration > 0 else 2
    source_rank = 0 if playable else 1
    return (
        suspicious_identity,
        source_rank,
        needs_review,
        duration_rank,
        variant_rank,
        metadata_noise,
        -canonical,
        -verified,
        -source_owner_match,
        -followers,
        -popularity,
        -quality,
    )


def _safe_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _replace_track_uploader_link(track, profile_artist) -> bool:
    """Keep uploader metadata aligned with the source URL actually stored.

    A provider reupload that merely deduplicates into an existing song must not
    become another displayed artist of that song.  This helper is used only
    when the track's source belongs to ``profile_artist``.
    """

    links = list(getattr(track, "artist_links", []) or [])
    uploader_links = [link for link in links if getattr(link, "role", "main") == "uploader"]
    main_artist_ids = {
        getattr(link, "artist_id", None) or getattr(getattr(link, "artist", None), "id", None)
        for link in links
        if getattr(link, "role", "main") == "main"
    }
    profile_id = getattr(profile_artist, "id", None)
    changed = False
    for link in uploader_links:
        link_artist_id = getattr(link, "artist_id", None) or getattr(getattr(link, "artist", None), "id", None)
        if profile_id in main_artist_ids or link_artist_id != profile_id:
            track.artist_links.remove(link)
            changed = True
    if profile_id not in main_artist_ids and not any(
        (getattr(link, "artist_id", None) or getattr(getattr(link, "artist", None), "id", None)) == profile_id
        and getattr(link, "role", "main") == "uploader"
        for link in track.artist_links
    ):
        track.artist_links.append(TrackArtist(artist=profile_artist, role="uploader"))
        changed = True
    return changed


def _suppress_title_identity_placeholders(items: list, query: str) -> list:
    """Hide title-shaped placeholder accounts when a complete original exists.

    A legitimate self-titled song is retained unless the candidate is both
    unauthoritative and incomplete.  The rule therefore removes the common
    zero-duration ``title == artist == query`` imports without hiding covers
    or different recordings that happen to share a title.
    """

    if not items:
        return items

    def duration(item) -> int:
        if isinstance(item, dict):
            return _safe_int(item.get("duration"))
        return _safe_int(getattr(item, "duration_seconds", 0))

    has_complete_original = any(
        _relevance_score(query, _item_title_text(item), _item_artist_text(item)) <= 1
        and _originality_sort_key(item, query)[0] == 0
        and duration(item) >= 30
        for item in items
    )
    if not has_complete_original:
        return items
    return [
        item
        for item in items
        if not (
            _originality_sort_key(item, query)[0] > 0
            and duration(item) < 30
        )
    ]


def _canonicalize_catalog_track_source(track) -> bool:
    name = (track.source_name or "").lower()
    if name not in {"youtube", "youtube_music", "yt"}:
        return False
    canonical_url = _canonical_youtube_music_url(track.source_url, track.source_external_id)
    if not canonical_url:
        return False
    changed = False
    if track.source_url != canonical_url:
        track.source_url = canonical_url
        changed = True
    if name != "youtube":
        track.source_name = "youtube"
        changed = True
    return changed


def _is_known_provider_source(source_name: str | None, source_url: str | None) -> bool:
    name = (source_name or "").lower()
    if name in {"soundcloud", "sc"}:
        return _is_soundcloud_source(source_url)
    if name in {"youtube", "youtube_music", "yt"}:
        return _is_youtube_music_source(source_url)
    return _is_soundcloud_source(source_url) or _is_youtube_music_source(source_url)


def _score_provider_result(query: str, entry: dict) -> int:
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("title", "uploader", "artist", "channel", "creator", "description", "webpage_url")
    ).lower()
    tokens = [token for token in normalize_name(query).split() if len(token) > 1]
    return sum(1 for token in tokens if token in haystack)


def _provider_query_relevance(query: str, entry: dict) -> int:
    tokens = _query_tokens(query)
    if not tokens:
        return 0
    title_raw = str(entry.get("title") or "")
    artist_raw = _entry_artist_name(entry, "")
    tier = _relevance_score(query, title_raw, artist_raw)
    if tier < 6:
        # Invert so that callers (who sort descending) rank tier-0 highest.
        return 7 - _relevance_bucket(tier)
    return 0


def _search_provider(query: str, provider: dict, limit: int) -> list[dict]:
    options = _yt_dlp_options(extract_flat=True)
    search_limit = max(1, min(EXTERNAL_PARSE_LIMIT, limit))
    search_url = (
        f"{provider['search_prefix']}{search_limit}:{query}"
        if provider.get("search_prefix")
        else str(provider["search_url"]).format(query=quote_plus(query))
    )
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(search_url, download=False)

    entries = [entry for entry in (info or {}).get("entries") or [] if isinstance(entry, dict)]
    provider_name = str(provider["name"])
    playable = [
        entry for entry in entries
        if _is_allowed_provider_entry(provider_name, entry) and _provider_query_relevance(query, entry) > 0
    ]
    seen = set()
    unique = []
    for entry in playable:
        key = str(entry.get("id") or _candidate_source_url(provider_name, entry))
        if not key or key in seen:
            continue
        seen.add(key)
        entry["_search_order"] = len(unique)
        unique.append(entry)
    if provider_name == "youtube":
        unique.sort(
            key=lambda entry: (
                _provider_query_relevance(query, entry),
                provider_authority_score(entry.get("title"), _entry_artist_name(entry, ""), query),
                -int(entry.get("_search_order") or 0),
            ),
            reverse=True,
        )
    else:
        unique.sort(
            key=lambda entry: (
                _provider_query_relevance(query, entry),
                provider_authority_score(entry.get("title"), _entry_artist_name(entry, ""), query),
                _score_provider_result(query, entry),
            ),
            reverse=True,
        )
    return unique[:search_limit]


def _entry_artist_name(entry: dict, fallback: str) -> str:
    return str(
        entry.get("artist")
        or entry.get("creator")
        or entry.get("uploader")
        or entry.get("channel")
        or fallback
    ).strip()


def _entry_artist_url(entry: dict) -> str | None:
    return entry.get("uploader_url") or entry.get("channel_url") or entry.get("creator_url")


def _ensure_track_tag(track, tag: str | None) -> bool:
    if not tag:
        return False
    try:
        tags = json.loads(track.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    string_tags = [str(item) for item in tags]
    if tag in string_tags:
        return False
    string_tags.append(tag)
    track.tags_json = json.dumps(string_tags)
    return True


def _save_provider_entry(db: Session, query: str, provider: dict, result: dict) -> bool:
    raw_title = (result.get("title") or query).strip()
    raw_artist_name = _entry_artist_name(result, query)
    title = title_without_artist_prefix(raw_title)
    artist_name = clean_provider_artist(raw_title, raw_artist_name, query)
    normalized_query = normalize_name(query)
    normalized_raw_artist = normalize_name(raw_artist_name)
    parsed_artist = artist_from_title(raw_title)
    if (
        artist_name
        and parsed_artist
        and normalize_name(parsed_artist) == normalized_query
        and normalized_raw_artist == normalized_query
    ):
        artist_name = clean_display_artist_name(query)
    provider_name = str(provider["name"])
    provider_score, provider_score_reliable = provider_popularity_score(
        view_count=result.get("view_count"),
        like_count=result.get("like_count"),
        repost_count=result.get("repost_count"),
        timestamp=result.get("timestamp"),
        fallback=float(provider["popularity_score"]),
    )
    source_url = _candidate_source_url(provider_name, result)
    cover_url = extract_cover_url(result, provider_name=provider_name)
    if not cover_url and provider_name == "soundcloud":
        cover_url = fetch_soundcloud_oembed_cover(source_url)
    if not title or not artist_name or not source_url or not _is_allowed_provider_entry(provider_name, result):
        return False

    uploader_profile = None
    profile_artist = None
    if provider_name == "soundcloud":
        uploader_profile = fetch_soundcloud_profile(_entry_artist_url(result) or "", timeout=5.0)
        if uploader_profile is not None:
            profile_artist, _profile_created = find_or_create_artist(
                db,
                name=uploader_profile.username,
                region=detect_artist_region(uploader_profile.username),
                avatar_url=uploader_profile.avatar_url,
                genres=[],
                source_name="soundcloud",
                source_external_id=uploader_profile.external_id,
                source_url=uploader_profile.permalink_url,
                source_followers_count=uploader_profile.followers_count,
                source_verified=uploader_profile.verified,
                is_canonical=True,
                profile_resolved_at=datetime.utcnow(),
                import_status="imported",
            )

    external_id = str(result.get("id") or source_url)
    existing = find_track_by_provider_external_id(db, provider=provider_name, external_id=external_id)
    if existing:
        changed = _canonicalize_catalog_track_source(existing)
        if provider_score_reliable and abs(float(existing.popularity_score or 0.0) - provider_score) >= 0.001:
            existing.popularity_score = provider_score
            changed = True
        if provider_score_reliable and _ensure_track_tag(existing, PROVIDER_POPULARITY_TAG):
            changed = True
        if (
            profile_artist is not None
            and source_profile_matches_artist(existing.source_url, profile_artist.name)
            and _replace_track_uploader_link(existing, profile_artist)
        ):
            changed = True
        current_artists = {
            normalize_name(link.artist.name)
            for link in existing.artist_links
            if getattr(link, "artist", None) and link.artist.name
        }
        if parsed_artist and normalize_name(parsed_artist) not in current_artists:
            parsed_matches_profile = bool(
                uploader_profile
                and normalize_name(uploader_profile.username) == normalize_name(parsed_artist)
            )
            repaired_artist, _created = find_or_create_artist(
                db,
                name=parsed_artist,
                region=detect_artist_region(parsed_artist),
                avatar_url=uploader_profile.avatar_url if parsed_matches_profile else None,
                genres=[],
                source_name=provider_name if parsed_matches_profile else None,
                source_external_id=uploader_profile.external_id if parsed_matches_profile else None,
                source_url=uploader_profile.permalink_url if parsed_matches_profile else None,
                source_followers_count=uploader_profile.followers_count if parsed_matches_profile else None,
                source_verified=uploader_profile.verified if parsed_matches_profile else None,
                is_canonical=True if parsed_matches_profile else None,
                profile_resolved_at=datetime.utcnow() if parsed_matches_profile else None,
                import_status="imported",
            )
            for link in list(existing.artist_links):
                if getattr(link, "role", "main") == "main":
                    existing.artist_links.remove(link)
            existing.artist_links.append(TrackArtist(artist=repaired_artist, role="main"))
            changed = True
        if existing.title != title:
            existing.title = title
            existing.normalized_title = normalize_title(title)
            changed = True
        if cover_url and not existing.cover_url:
            existing.cover_url = cover_url
            changed = True
        if changed:
            db.add(existing)
            db.commit()
        return True

    duration_seconds = _safe_int(result.get("duration"))
    normalized_artist = normalize_name(artist_name)
    duplicate = find_duplicate_track_for_artist(
        db,
        normalized_artist=normalized_artist,
        title=title,
        duration_seconds=duration_seconds,
    )
    if duplicate:
        popularity_changed = provider_score_reliable and abs(
            float(duplicate.popularity_score or 0.0) - provider_score
        ) >= 0.001
        if popularity_changed:
            duplicate.popularity_score = provider_score
        popularity_tag_changed = provider_score_reliable and _ensure_track_tag(
            duplicate,
            PROVIDER_POPULARITY_TAG,
        )
        if not duplicate.is_playable or not _is_known_provider_source(duplicate.source_name, duplicate.source_url):
            duplicate.is_playable = True
            duplicate.source_name = provider_name
            duplicate.source_external_id = external_id
            duplicate.source_url = str(source_url)
            duplicate.cover_url = duplicate.cover_url or cover_url
            duplicate.genre = duplicate.genre or result.get("genre") or None
            if profile_artist is not None:
                _replace_track_uploader_link(duplicate, profile_artist)
            db.add(duplicate)
            db.commit()
        else:
            changed = _canonicalize_catalog_track_source(duplicate)
            if popularity_changed:
                changed = True
            if popularity_tag_changed:
                changed = True
            if cover_url and not duplicate.cover_url:
                duplicate.cover_url = cover_url
                changed = True
            if changed:
                db.add(duplicate)
                db.commit()
        return True

    profile_matches_artist = bool(
        uploader_profile
        and normalize_name(uploader_profile.username) == normalize_name(artist_name)
    )
    artist, _created = find_or_create_artist(
        db,
        name=artist_name,
        region=detect_artist_region(artist_name),
        avatar_url=uploader_profile.avatar_url if profile_matches_artist else None,
        genres=[],
        source_name=provider_name if profile_matches_artist else None,
        source_external_id=uploader_profile.external_id if profile_matches_artist else None,
        source_url=uploader_profile.permalink_url if profile_matches_artist else None,
        source_followers_count=uploader_profile.followers_count if profile_matches_artist else None,
        source_verified=uploader_profile.verified if profile_matches_artist else None,
        is_canonical=True if profile_matches_artist else None,
        profile_resolved_at=datetime.utcnow() if profile_matches_artist else None,
        import_status="imported",
    )
    payload = TrackSeedCreate(
        title=title,
        artist=artist_name,
        duration_seconds=duration_seconds,
        cover_url=cover_url,
        genre=result.get("genre") or None,
        tags=[
            "provider",
            str(provider["tag"]),
            *([PROVIDER_POPULARITY_TAG] if provider_score_reliable else []),
        ],
        region=detect_artist_region(artist_name),
        popularity_score=provider_score,
        quality_score=100.0,
        is_playable=True,
        audio_src=None,
        source_name=provider_name,
        source_external_id=external_id,
        source_url=str(source_url),
        needs_review=False,
    )
    track = create_track_with_artist(db, payload, artist)
    if profile_artist is not None and profile_artist.id != artist.id:
        ensure_track_artist_link(
            db,
            track_id=track.id,
            artist_id=profile_artist.id,
            role="uploader",
        )
    db.commit()
    return True


def _save_provider_tracks(
    db: Session,
    query: str,
    provider: dict,
    dedupe_groups: list[dict],
    remaining: int,
) -> int:
    accepted = 0
    provider_entries = _prefer_title_matches(_search_provider(query, provider, limit=EXTERNAL_PARSE_LIMIT), query)
    for entry_index, entry in enumerate(provider_entries):
        if accepted >= remaining:
            break
        counted_for_results = _take_with_song_dedupe(dedupe_groups, entry)
        # The existing catalog can contain low-quality duplicates that already
        # fill a song quota. Still inspect the provider's best candidates so an
        # authoritative result can be added or repair old metadata.
        if not counted_for_results and entry_index >= 10:
            continue
        try:
            if _save_provider_entry(db, query, provider, entry):
                if counted_for_results:
                    accepted += 1
        except Exception:
            db.rollback()
    return accepted


def _save_external_tracks(db: Session, query: str, existing_results: list | None = None) -> None:
    dedupe_groups = _dedupe_groups_from_items(existing_results or [])
    accepted_total = 0
    for provider in SEARCH_PROVIDERS:
        remaining = EXTERNAL_PARSE_LIMIT - accepted_total
        if remaining <= 0:
            return
        try:
            _log(f"[SEARCH HYDRATE] provider={provider['name']} start key={_query_log_key(query)}")
            stored = _save_provider_tracks(db, query, provider, dedupe_groups, remaining)
        except Exception:
            db.rollback()
            stored = 0
        accepted_total += stored
        _log(
            f"[SEARCH HYDRATE] provider={provider['name']} stored={stored} "
            f"key={_query_log_key(query)}"
        )
        if accepted_total >= EXTERNAL_PARSE_LIMIT:
            return


def _catalog_candidates(db: Session, query: str, limit: int) -> list:
    direct = search_tracks(db, query, limit=limit)
    fuzzy = search_track_fuzzy_candidates(db, query, limit=limit) if len(direct) < limit else []
    merged = []
    seen_ids: set[int] = set()
    for track in [*direct, *fuzzy]:
        if track.id in seen_ids:
            continue
        seen_ids.add(track.id)
        merged.append(track)
    return merged


def hydrate_search_catalog(query: str) -> None:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return
    if not _hydration_slots.acquire(blocking=False):
        with _hydration_lock:
            _scheduled_queries.discard(normalized_query)
        return
    with _hydration_lock:
        if normalized_query in _hydrating_queries:
            _hydration_slots.release()
            return
        _scheduled_queries.add(normalized_query)
        _hydrating_queries.add(normalized_query)

    try:
        with SessionLocal() as db:
            candidates = [
                track
                for track in _catalog_candidates(db, query, limit=SEARCH_RESULT_LIMIT * 2)
                if _is_clean_catalog_track(track) and _catalog_track_matches_query(track, query)
            ]
            candidates = _suppress_title_identity_placeholders(
                _prefer_title_matches(candidates, query),
                query,
            )
            local_results = _apply_variant_quota(
                candidates
            )
            _log(f"[SEARCH HYDRATE] start key={_query_log_key(query)} local={len(local_results)}")
            _save_external_tracks(db, query, existing_results=local_results)
    finally:
        with _hydration_lock:
            _hydrating_queries.discard(normalized_query)
            _scheduled_queries.discard(normalized_query)
            _hydrated_queries[normalized_query] = time.monotonic()
            if len(_hydrated_queries) > 2048:
                oldest = sorted(_hydrated_queries, key=_hydrated_queries.get)[:512]
                for cache_key in oldest:
                    _hydrated_queries.pop(cache_key, None)
        _hydration_slots.release()


def _schedule_hydration(query: str, background_tasks: BackgroundTasks | None) -> None:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return
    with _hydration_lock:
        if normalized_query in _hydrating_queries or normalized_query in _scheduled_queries:
            return
        hydrated_at = _hydrated_queries.get(normalized_query)
        if hydrated_at and time.monotonic() - hydrated_at < HYDRATION_COOLDOWN_SECONDS:
            return
        _scheduled_queries.add(normalized_query)
    try:
        if background_tasks:
            background_tasks.add_task(hydrate_search_catalog, query)
        else:
            thread = threading.Thread(target=hydrate_search_catalog, args=(query,), daemon=True)
            thread.start()
    except Exception:
        with _hydration_lock:
            _scheduled_queries.discard(normalized_query)
        raise


def search_hydration_pending(query: str) -> bool:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return False
    with _hydration_lock:
        return normalized_query in _scheduled_queries or normalized_query in _hydrating_queries


def search_local_catalog(
    db: Session,
    query: str,
    background_tasks: BackgroundTasks | None = None,
    limit: int = SEARCH_RESULT_LIMIT,
) -> list[TrackRead]:
    safe_limit = max(1, min(limit, SEARCH_RESULT_LIMIT))
    if not normalize_search_text(query):
        return []
    candidate_limit = min(SEARCH_RESULT_LIMIT * 2, max(safe_limit * 2, safe_limit + 20))
    candidates = [
        track
        for track in _catalog_candidates(db, query, limit=candidate_limit)
        if _is_clean_catalog_track(track) and _catalog_track_matches_query(track, query)
    ]
    candidates = _suppress_title_identity_placeholders(
        _prefer_title_matches(candidates, query),
        query,
    )
    local_results = _apply_variant_quota(
        candidates,
        limit=safe_limit,
    )
    changed = False
    for track in local_results:
        if _canonicalize_catalog_track_source(track):
            changed = True
    if changed:
        for track in local_results:
            db.add(track)
        db.commit()
    _schedule_hydration(query, background_tasks)
    return [track_to_read(track) for track in local_results]
