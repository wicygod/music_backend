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


def test_cover_proxy_accepts_soundcloud_avatar_hosts_and_has_fallbacks() -> None:
    assert _is_allowed_host("a1.sndcdn.com")
    assert _is_allowed_host("cf-media.sndcdn.com")

    soundcloud = "https://i1.sndcdn.com/artworks-demo-t500x500.jpg"
    youtube = "https://i.ytimg.com/vi/demo/hqdefault.jpg"
    assert _image_candidates(soundcloud) == [soundcloud, soundcloud.replace("-t500x500.", "-large.")]
    assert _image_candidates(youtube) == [youtube, youtube.replace("/hqdefault.", "/mqdefault.")]
