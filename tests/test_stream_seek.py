from starlette.requests import Request

from app.routers.images import _image_candidates, _is_allowed_host
from app.routers.stream import MP3_BYTES_PER_SECOND, _mp3_stream_headers, _seek_context


def request_with_range(value: str | None = None) -> Request:
    headers = [] if value is None else [(b"range", value.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_explicit_timeline_seek_keeps_its_logical_offset_when_webview_sends_range_zero() -> None:
    start_seconds, range_start, total_bytes, status_code = _seek_context(
        request_with_range("bytes=0-"),
        start=92.5,
        duration_seconds=240,
    )

    assert start_seconds == 92.5
    assert range_start is None
    assert status_code == 206
    assert total_bytes == 240 * MP3_BYTES_PER_SECOND
    headers = _mp3_stream_headers(int(start_seconds * MP3_BYTES_PER_SECOND), total_bytes, status_code)
    assert headers["Content-Range"].startswith(f"bytes {int(start_seconds * MP3_BYTES_PER_SECOND)}-")
    assert headers["Accept-Ranges"] == "bytes"


def test_native_byte_range_remains_supported_for_initial_stream() -> None:
    byte_offset = MP3_BYTES_PER_SECOND * 10
    start_seconds, range_start, total_bytes, status_code = _seek_context(
        request_with_range(f"bytes={byte_offset}-"),
        start=0,
        duration_seconds=120,
    )

    assert start_seconds == 10
    assert range_start == byte_offset
    assert status_code == 206
    headers = _mp3_stream_headers(byte_offset, total_bytes, status_code)
    assert headers["Content-Range"].startswith(f"bytes {byte_offset}-")


def test_cover_proxy_accepts_soundcloud_avatar_hosts_and_has_fallbacks() -> None:
    assert _is_allowed_host("a1.sndcdn.com")
    assert _is_allowed_host("cf-media.sndcdn.com")

    soundcloud = "https://i1.sndcdn.com/artworks-demo-t500x500.jpg"
    youtube = "https://i.ytimg.com/vi/demo/hqdefault.jpg"
    assert _image_candidates(soundcloud) == [soundcloud, soundcloud.replace("-t500x500.", "-large.")]
    assert _image_candidates(youtube) == [youtube, youtube.replace("/hqdefault.", "/mqdefault.")]
