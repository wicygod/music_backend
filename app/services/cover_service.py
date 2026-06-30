from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx


def extract_cover_url(entry: dict | None, *, provider_name: str | None = None) -> str | None:
    if not isinstance(entry, dict):
        return None

    for key in ("artwork_url", "album_artwork_url", "thumbnail", "display_thumbnail", "cover"):
        url = _clean_url(entry.get(key))
        if url:
            return _normalize_cover_url(url, provider_name=provider_name)

    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        best = _best_thumbnail(thumbnails)
        if best:
            return _normalize_cover_url(best, provider_name=provider_name)

    images = entry.get("images")
    if isinstance(images, list):
        best = _best_thumbnail(images)
        if best:
            return _normalize_cover_url(best, provider_name=provider_name)

    if (provider_name or "").lower() in {"youtube", "youtube_music", "yt"}:
        video_id = str(entry.get("id") or "").strip()
        if video_id:
            return f"https://i.ytimg.com/vi/{quote(video_id, safe='')}/hqdefault.jpg"
    return None


def cover_url_for_client(url: str | None) -> str | None:
    cleaned = _clean_url(url)
    if not cleaned:
        return None
    if cleaned.startswith("data:") or cleaned.startswith("/"):
        return cleaned
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return f"/api/images/proxy?url={quote(cleaned, safe='')}"
    return cleaned


def fetch_soundcloud_oembed_cover(source_url: str | None) -> str | None:
    cleaned = _clean_url(source_url)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "soundcloud.com" and not host.endswith(".soundcloud.com"):
        return None
    try:
        response = httpx.get(
            "https://soundcloud.com/oembed",
            params={"format": "json", "url": cleaned},
            timeout=8.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    return _normalize_cover_url(_clean_url(payload.get("thumbnail_url")) or "", provider_name="soundcloud") or None


def _best_thumbnail(items: list) -> str | None:
    candidates: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _clean_url(item.get("url") or item.get("src"))
        if not url:
            continue
        width = _safe_int(item.get("width"))
        height = _safe_int(item.get("height"))
        preference = width * height
        if "maxresdefault" in url:
            preference += 10_000_000
        elif "hqdefault" in url:
            preference += 5_000_000
        candidates.append((preference, url))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _normalize_cover_url(url: str, *, provider_name: str | None = None) -> str:
    if (provider_name or "").lower() in {"soundcloud", "sc"}:
        return (
            url.replace("-large.", "-t500x500.")
            .replace("-t300x300.", "-t500x500.")
            .replace("-t67x67.", "-t500x500.")
        )
    return url


def _clean_url(value) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith(("http://", "https://", "data:", "/")):
        return None
    if url.startswith(("http://", "https://")):
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
    return url


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
