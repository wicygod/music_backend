from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response


router = APIRouter(prefix="/api/images", tags=["images"])

ALLOWED_IMAGE_HOSTS = (
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
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Image fetch failed")

    content_type = response.headers.get("content-type") or "image/jpeg"
    if not content_type.lower().startswith("image/"):
        raise HTTPException(status_code=502, detail="Remote URL is not an image")

    return Response(
        content=response.content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _is_allowed_host(host: str) -> bool:
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_IMAGE_HOSTS)
