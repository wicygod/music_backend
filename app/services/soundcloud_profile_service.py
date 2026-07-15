from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.services.normalization_service import normalize_artist_name


_HYDRATION_ASSIGNMENT_RE = re.compile(r"(?:window\.)?__sc_hydration\s*=", re.IGNORECASE)
_PROFILE_NAME_SPLIT_RE = re.compile(r"\s*(?:&|/|\+|,|\bfeat\.?\b|\bft\.?\b|\bx\b)\s*", re.IGNORECASE)
_RESERVED_PROFILE_PATHS = {
    "charts",
    "discover",
    "messages",
    "search",
    "settings",
    "stream",
    "upload",
    "you",
}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class SoundCloudProfile:
    """Public SoundCloud profile metadata used to identify a catalog artist."""

    id: str | None
    username: str
    permalink_url: str
    avatar_url: str | None
    followers_count: int = 0
    verified: bool = False
    track_count: int = 0
    urn: str | None = None
    source: Literal["hydration", "oembed"] = "hydration"

    @property
    def external_id(self) -> str | None:
        """Prefer the forward-compatible URN over SoundCloud's deprecated numeric id."""

        return self.urn or self.id


def parse_soundcloud_profile_hydration(
    html: str,
    *,
    source_url: str | None = None,
) -> SoundCloudProfile | None:
    """Parse the public ``window.__sc_hydration`` user payload.

    ``json.JSONDecoder.raw_decode`` is deliberately used instead of a broad
    regular expression. Hydration JSON can be large and can contain arbitrary
    text in profile descriptions.
    """

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

    expected_url = canonical_soundcloud_profile_url(source_url)
    user_payloads: list[dict] = []
    for item in hydrated:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if item.get("hydratable") == "user" or data.get("kind") == "user":
            user_payloads.append(data)

    # The profile represented by the page is normally the last hydrated user.
    # Still validate its permalink when the caller supplied a profile URL so an
    # authenticated/session user cannot accidentally win.
    for data in reversed(user_payloads):
        profile = _profile_from_user_payload(data, source="hydration")
        if profile is None:
            continue
        if expected_url and canonical_soundcloud_profile_url(profile.permalink_url) != expected_url:
            continue
        return profile
    return None


def fetch_soundcloud_profile(
    profile_url: str,
    *,
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> SoundCloudProfile | None:
    """Fetch a public profile, falling back to SoundCloud's official oEmbed.

    oEmbed does not expose follower or verification counts, but it still gives
    a trustworthy profile avatar/name when the hydration payload changes.
    """

    canonical_url = canonical_soundcloud_profile_url(profile_url)
    if canonical_url is None:
        return None
    safe_timeout = max(0.5, min(float(timeout), 30.0))

    if client is not None:
        return _fetch_soundcloud_profile(client, canonical_url, safe_timeout)

    return _fetch_soundcloud_profile_cached(canonical_url, round(safe_timeout, 1))


@lru_cache(maxsize=1024)
def _fetch_soundcloud_profile_cached(
    canonical_url: str,
    timeout: float,
) -> SoundCloudProfile | None:
    with httpx.Client(
        timeout=timeout / 2,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    ) as owned_client:
        return _fetch_soundcloud_profile(owned_client, canonical_url, timeout)


def search_soundcloud_profile_urls(
    query: str,
    *,
    limit: int = 10,
    timeout: float = 6.0,
) -> list[str]:
    """Discover a bounded set of uploader profiles through yt-dlp ``scsearch``.

    A subprocess provides a hard wall-clock timeout and isolates provider
    extractor failures from the API process. No SoundCloud credentials or
    browser client id are persisted by this service.
    """

    clean_query = str(query or "").strip()
    if not normalize_artist_name(clean_query):
        return []
    safe_limit = max(1, min(int(limit), 20))
    safe_timeout = max(1.0, min(float(timeout), 20.0))
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
        str(max(1, int(safe_timeout / 2))),
        "--retries",
        "0",
        "--extractor-retries",
        "0",
        f"scsearch{safe_limit}:{clean_query}",
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=safe_timeout,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for entry in entries[:safe_limit]:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("uploader_url") or entry.get("channel_url") or entry.get("webpage_url")
        canonical = canonical_soundcloud_profile_url(str(candidate or ""))
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        urls.append(canonical)
    return urls


def resolve_canonical_soundcloud_profile(
    query: str,
    candidate_profile_urls: Iterable[str] = (),
    *,
    include_search: bool = True,
    max_candidates: int = 12,
    timeout: float = 12.0,
) -> SoundCloudProfile | None:
    """Resolve one relevant profile using exact name, verification and reach.

    Exact normalized username matches always outrank compound/fuzzy matches.
    Within the same match class, verified profiles and then follower counts win.
    This prevents a high-follower unrelated uploader from becoming an artist.
    """

    normalized_query = normalize_artist_name(str(query or ""))
    if not normalized_query:
        return None
    safe_max_candidates = max(1, min(int(max_candidates), 20))
    total_timeout = max(1.0, min(float(timeout), 30.0))
    deadline = time.monotonic() + total_timeout

    discovered: list[str] = []
    if include_search:
        search_timeout = min(6.0, max(1.0, total_timeout * 0.4))
        discovered = search_soundcloud_profile_urls(
            query,
            limit=safe_max_candidates,
            timeout=search_timeout,
        )

    # Bound discovery and catalog candidates independently. Previously the
    # search results were appended first and could consume the entire shared
    # limit, silently dropping the artist's already-known canonical URL. That
    # allowed a zero-reach exact-name account from scsearch to replace a much
    # more popular existing profile during a seed refresh. Keeping both
    # bounded pools means every fetched profile is still compared by the same
    # verification/follower ranking; neither its origin nor list position can
    # make a legacy URL win.
    ordered_urls: list[str] = []
    seen: set[str] = set()

    def append_bounded(raw_urls: Iterable[str]) -> None:
        added = 0
        for raw_url in raw_urls:
            canonical = canonical_soundcloud_profile_url(str(raw_url or ""))
            if canonical is None or canonical in seen:
                continue
            seen.add(canonical)
            ordered_urls.append(canonical)
            added += 1
            if added >= safe_max_candidates:
                break

    append_bounded(discovered)
    append_bounded(candidate_profile_urls)

    relevant_profiles: list[tuple[int, SoundCloudProfile]] = []
    remaining = max(0.5, deadline - time.monotonic())
    worker_count = max(1, min(6, len(ordered_urls)))
    request_budget = max(0.5, min(3.0, remaining))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="soundcloud-profile") as pool:
        profiles = list(
            pool.map(
                lambda url: fetch_soundcloud_profile(url, timeout=request_budget),
                ordered_urls,
            )
        )
    for profile in profiles:
        if profile is None:
            continue
        match_rank = _name_match_rank(normalized_query, profile.username)
        if match_rank <= 0:
            continue
        relevant_profiles.append((match_rank, profile))

    if not relevant_profiles:
        return None
    return max(
        relevant_profiles,
        key=lambda item: (
            item[0],
            bool(item[1].verified),
            max(0, int(item[1].followers_count)),
            max(0, int(item[1].track_count)),
            not _is_default_avatar(item[1].avatar_url),
            item[1].permalink_url,
        ),
    )[1]


def _fetch_soundcloud_profile(
    client: httpx.Client,
    canonical_url: str,
    timeout: float,
) -> SoundCloudProfile | None:
    request_timeout = max(0.25, timeout / 2)
    try:
        response = client.get(
            canonical_url,
            timeout=request_timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        response.raise_for_status()
        profile = parse_soundcloud_profile_hydration(response.text, source_url=canonical_url)
        if profile is not None:
            return profile
    except (httpx.HTTPError, ValueError):
        pass

    try:
        response = client.get(
            "https://soundcloud.com/oembed",
            params={"format": "json", "url": canonical_url},
            timeout=request_timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _profile_from_oembed(payload, expected_url=canonical_url)


def _profile_from_user_payload(
    data: dict,
    *,
    source: Literal["hydration", "oembed"],
) -> SoundCloudProfile | None:
    username = str(data.get("username") or "").strip()
    permalink_url = str(data.get("permalink_url") or "").strip()
    if not permalink_url and data.get("permalink"):
        permalink_url = f"https://soundcloud.com/{str(data['permalink']).strip('/')}"
    canonical_url = canonical_soundcloud_profile_url(permalink_url)
    if not username or canonical_url is None:
        return None
    raw_id = data.get("id")
    urn = str(data.get("urn") or "").strip() or None
    return SoundCloudProfile(
        id=str(raw_id) if raw_id is not None else None,
        urn=urn,
        username=username,
        permalink_url=canonical_url,
        avatar_url=_normalize_avatar_url(data.get("avatar_url")),
        followers_count=_safe_non_negative_int(data.get("followers_count")),
        verified=bool(data.get("verified")),
        track_count=_safe_non_negative_int(data.get("track_count")),
        source=source,
    )


def _profile_from_oembed(payload: dict, *, expected_url: str) -> SoundCloudProfile | None:
    username = str(payload.get("author_name") or payload.get("title") or "").strip()
    raw_url = str(payload.get("author_url") or expected_url).strip()
    canonical_url = canonical_soundcloud_profile_url(raw_url)
    if not username or canonical_url is None or canonical_url != expected_url:
        return None
    return SoundCloudProfile(
        id=None,
        urn=None,
        username=username,
        permalink_url=canonical_url,
        avatar_url=_normalize_avatar_url(payload.get("thumbnail_url")),
        source="oembed",
    )


def canonical_soundcloud_profile_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or host != "soundcloud.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0].lower() in _RESERVED_PROFILE_PATHS:
        return None
    return urlunsplit(("https", "soundcloud.com", f"/{parts[0]}", "", ""))


def _normalize_avatar_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return re.sub(
        r"-(?:mini|tiny|small|badge|t67x67|large|t300x300|crop)\.(jpg|png)$",
        r"-t500x500.\1",
        raw,
        flags=re.IGNORECASE,
    )


def _safe_non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _name_match_rank(normalized_query: str, username: str) -> int:
    normalized_username = normalize_artist_name(username)
    if not normalized_username:
        return 0
    if normalized_username == normalized_query:
        return 3
    segments = {
        normalize_artist_name(part)
        for part in _PROFILE_NAME_SPLIT_RE.split(username)
        if normalize_artist_name(part)
    }
    if normalized_query in segments:
        return 2
    query_tokens = normalized_query.split()
    username_tokens = normalized_username.split()
    if query_tokens and all(token in username_tokens for token in query_tokens):
        return 1
    return 0


def _is_default_avatar(value: str | None) -> bool:
    return not value or "default_avatar" in value.lower()
