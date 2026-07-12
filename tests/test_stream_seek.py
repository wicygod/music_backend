import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import stream as stream_router
from app.routers.images import _image_candidates, _is_allowed_host
from app.routers.stream import MIN_CACHED_AUDIO_BYTES, _audio_cache_path, _cached_audio_is_valid


def test_audio_cache_uses_stable_distinct_file_names() -> None:
    first = _audio_cache_path("track:1:https://soundcloud.com/example/one")
    same = _audio_cache_path("track:1:https://soundcloud.com/example/one")
    second = _audio_cache_path("track:2:https://soundcloud.com/example/two")

    assert first == same
    assert first != second
    assert first.suffix == ".mp3"


def test_audio_cache_rejects_partial_files(tmp_path) -> None:
    partial = tmp_path / "partial.mp3"
    partial.write_bytes(b"x" * (MIN_CACHED_AUDIO_BYTES - 1))
    complete = tmp_path / "complete.mp3"
    complete.write_bytes(b"x" * MIN_CACHED_AUDIO_BYTES)

    assert not _cached_audio_is_valid(partial)
    assert _cached_audio_is_valid(complete)


def test_audio_cache_build_is_deduplicated(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(stream_router, "AUDIO_CACHE_DIR", tmp_path)
    stream_router._audio_cache_tasks.clear()
    build_calls = 0

    async def fake_build(stream_url, cache_path):
        nonlocal build_calls
        build_calls += 1
        await asyncio.sleep(0.01)
        cache_path.write_bytes(b"x" * MIN_CACHED_AUDIO_BYTES)
        return cache_path

    monkeypatch.setattr(stream_router, "_build_cached_mp3", fake_build)

    async def prepare_twice():
        return await asyncio.gather(
            stream_router._ensure_cached_mp3("https://example.test/audio", "track:7:source"),
            stream_router._ensure_cached_mp3("https://example.test/audio", "track:7:source"),
        )

    first, second = asyncio.run(prepare_twice())

    assert first == second
    assert build_calls == 1
    assert _cached_audio_is_valid(first)
    assert not stream_router._audio_cache_tasks


def test_prepare_track_returns_cache_metadata_without_audio(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(stream_router, "AUDIO_CACHE_DIR", tmp_path)
    track = SimpleNamespace(id=17, duration_seconds=180)
    source_url = "https://soundcloud.com/example/track"
    stream_url = "https://cdn.example.test/audio.m3u8"
    expected_key = stream_router._track_audio_cache_key(track.id, source_url)

    monkeypatch.setattr(stream_router, "get_track", lambda db, track_id: track if track_id == track.id else None)

    async def fake_resolve_source(resolved_track, db):
        assert resolved_track is track
        return source_url

    async def fake_resolve_stream(resolved_track, db, resolved_source_url=None):
        assert resolved_track is track
        assert resolved_source_url == source_url
        return source_url, stream_url

    async def fake_ensure(resolved_stream_url, cache_key):
        assert resolved_stream_url == stream_url
        assert cache_key == expected_key
        cache_path = stream_router._audio_cache_path(cache_key)
        cache_path.write_bytes(b"x" * MIN_CACHED_AUDIO_BYTES)
        return cache_path

    monkeypatch.setattr(stream_router, "_resolve_track_source", fake_resolve_source)
    monkeypatch.setattr(stream_router, "_resolve_track_stream", fake_resolve_stream)
    monkeypatch.setattr(stream_router, "_ensure_cached_mp3", fake_ensure)

    request = SimpleNamespace(state=SimpleNamespace(user_id=5))
    result = asyncio.run(stream_router.prepare_track(track.id, request, db=object()))

    assert result == {
        "track_id": track.id,
        "status": "ready",
        "cache_hit": False,
        "size_bytes": MIN_CACHED_AUDIO_BYTES,
    }


def test_prepare_track_requires_authenticated_request() -> None:
    request = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(HTTPException) as error:
        asyncio.run(stream_router.prepare_track(1, request, db=object()))

    assert error.value.status_code == 401


def test_cover_proxy_accepts_soundcloud_avatar_hosts_and_has_fallbacks() -> None:
    assert _is_allowed_host("a1.sndcdn.com")
    assert _is_allowed_host("cf-media.sndcdn.com")

    soundcloud = "https://i1.sndcdn.com/artworks-demo-t500x500.jpg"
    youtube = "https://i.ytimg.com/vi/demo/hqdefault.jpg"
    assert _image_candidates(soundcloud) == [soundcloud, soundcloud.replace("-t500x500.", "-large.")]
    assert _image_candidates(youtube) == [youtube, youtube.replace("/hqdefault.", "/mqdefault.")]
