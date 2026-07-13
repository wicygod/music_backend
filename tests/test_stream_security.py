import asyncio

import pytest
from fastapi import HTTPException

from app.routers.stream import stream, stream_proxy


def test_arbitrary_stream_and_proxy_urls_are_disabled() -> None:
    with pytest.raises(HTTPException) as stream_error:
        asyncio.run(stream(request=None, url="http://127.0.0.1/private", start=0))  # type: ignore[arg-type]
    assert stream_error.value.status_code == 410

    with pytest.raises(HTTPException) as proxy_error:
        asyncio.run(
            stream_proxy(
                request=None,  # type: ignore[arg-type]
                segment_url="http://169.254.169.254/latest/meta-data",
                url=None,
                start=0,
            )
        )
    assert proxy_error.value.status_code == 410
