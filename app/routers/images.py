import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response


router = APIRouter(prefix="/api/images", tags=["images"])

ALLOWED_IMAGE_HOSTS = (
    "sndcdn.com",
    "i1.sndcdn.com",
    "i2.sndcdn.com",
    "i3.sndcdn.com",
    "i4.sndcdn.com",
    "i.ytimg.com",
    "yt3.ggpht.com",
    "lh3.googleusercontent.com",
    "is1-ssl.mzstatic.com",
    "is2-ssl.mzstatic.com",
    "is3-ssl.mzstatic.com",
    "is4-ssl.mzstatic.com",
    "is5-ssl.mzstatic.com",
)
RETRYABLE_IMAGE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@router.get("/proxy")
async def image_proxy(url: str = Query(..., min_length=8, max_length=2048)) -> Response:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not _is_allowed_host(host):
        raise HTTPException(status_code=400, detail="Unsupported image host")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": f"{parsed.scheme}://{host}/",
    }
    response: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        for candidate_url in _image_candidates(url):
            for attempt in range(3):
                try:
                    candidate_response = await client.get(candidate_url)
                except httpx.HTTPError:
                    if attempt == 2:
                        break
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                content_type = (candidate_response.headers.get("content-type") or "").lower()
                final_host = (candidate_response.url.host or "").lower()
                content_length = int(candidate_response.headers.get("content-length") or 0)
                if (
                    candidate_response.status_code < 400
                    and content_type.startswith("image/")
                    and _is_allowed_host(final_host)
                    and content_length <= MAX_IMAGE_BYTES
                    and len(candidate_response.content) <= MAX_IMAGE_BYTES
                ):
                    response = candidate_response
                    break
                if candidate_response.status_code not in RETRYABLE_IMAGE_STATUSES:
                    break
                await asyncio.sleep(0.2 * (attempt + 1))
            if response is not None:
                break

    if response is None:
        raise HTTPException(status_code=502, detail="Image fetch failed")

    content_type = response.headers.get("content-type") or "image/jpeg"

    return Response(
        content=response.content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "CDN-Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _is_allowed_host(host: str) -> bool:
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_IMAGE_HOSTS)


def _image_candidates(url: str) -> list[str]:
    candidates = [url]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "sndcdn.com" or host.endswith(".sndcdn.com"):
        for source_size in ("-t500x500.", "-t300x300.", "-large."):
            if source_size in url:
                fallback = url.replace(source_size, "-large.")
                if fallback not in candidates:
                    candidates.append(fallback)
                break
    if host == "i.ytimg.com" and "/hqdefault." in url:
        fallback = url.replace("/hqdefault.", "/mqdefault.")
        if fallback not in candidates:
            candidates.append(fallback)
    return candidates
