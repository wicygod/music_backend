from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from app.services import soundcloud_profile_service as service
from app.services.soundcloud_profile_service import SoundCloudProfile


def _hydration_html(**overrides) -> str:
    user = {
        "id": 1123647619,
        "urn": "soundcloud:users:1123647619",
        "kind": "user",
        "username": "Кишлак☆",
        "permalink": "kishlak",
        "permalink_url": "https://soundcloud.com/kishlak",
        "avatar_url": "https://i1.sndcdn.com/avatars-test-large.jpg",
        "followers_count": 105_366,
        "verified": True,
        "track_count": 296,
    }
    user.update(overrides)
    payload = [
        {"hydratable": "features", "data": {"features": ["example"]}},
        {"hydratable": "user", "data": user},
    ]
    return f"<script>window.__sc_hydration = {json.dumps(payload, ensure_ascii=False)};</script>"


def _profile(
    username: str,
    url: str,
    *,
    followers: int,
    verified: bool = False,
) -> SoundCloudProfile:
    return SoundCloudProfile(
        id=url.rsplit("/", 1)[-1],
        urn=f"soundcloud:users:{url.rsplit('/', 1)[-1]}",
        username=username,
        permalink_url=url,
        avatar_url=f"https://i1.sndcdn.com/avatars-{followers}-t500x500.jpg",
        followers_count=followers,
        verified=verified,
        track_count=20,
    )


def test_parse_public_hydration_reads_canonical_profile_fields() -> None:
    profile = service.parse_soundcloud_profile_hydration(
        _hydration_html(),
        source_url="https://soundcloud.com/kishlak/some-track",
    )

    assert profile is not None
    assert profile.id == "1123647619"
    assert profile.external_id == "soundcloud:users:1123647619"
    assert profile.username == "Кишлак☆"
    assert profile.permalink_url == "https://soundcloud.com/kishlak"
    assert profile.avatar_url == "https://i1.sndcdn.com/avatars-test-t500x500.jpg"
    assert profile.followers_count == 105_366
    assert profile.verified is True
    assert profile.track_count == 296
    assert profile.source == "hydration"


def test_parse_hydration_rejects_a_different_session_user() -> None:
    html = _hydration_html(permalink="somebody", permalink_url="https://soundcloud.com/somebody")

    assert (
        service.parse_soundcloud_profile_hydration(
            html,
            source_url="https://soundcloud.com/kishlak",
        )
        is None
    )


def test_fetch_profile_uses_oembed_when_hydration_is_unavailable() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/kishlak":
            return httpx.Response(200, text="<html>no hydration</html>")
        assert request.url.path == "/oembed"
        return httpx.Response(
            200,
            json={
                "author_name": "Кишлак☆",
                "author_url": "https://soundcloud.com/kishlak",
                "thumbnail_url": "https://i1.sndcdn.com/avatars-real-large.jpg",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        profile = service.fetch_soundcloud_profile(
            "https://soundcloud.com/kishlak",
            client=client,
        )

    assert calls == ["/kishlak", "/oembed"]
    assert profile is not None
    assert profile.username == "Кишлак☆"
    assert profile.avatar_url == "https://i1.sndcdn.com/avatars-real-t500x500.jpg"
    assert profile.followers_count == 0
    assert profile.source == "oembed"


def test_fetch_profile_does_not_request_oembed_after_valid_hydration() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=_hydration_html())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        profile = service.fetch_soundcloud_profile(
            "https://soundcloud.com/kishlak",
            client=client,
        )

    assert calls == 1
    assert profile is not None
    assert profile.source == "hydration"


def test_resolver_prefers_exact_normalized_name_before_more_followers(monkeypatch) -> None:
    profiles = {
        "https://soundcloud.com/kaiangel-9mice": _profile(
            "Kai Angel & 9mice",
            "https://soundcloud.com/kaiangel-9mice",
            followers=84_460,
            verified=True,
        ),
        "https://soundcloud.com/4ngelkai": _profile(
            "kai angel",
            "https://soundcloud.com/4ngelkai",
            followers=38_602,
        ),
    }
    monkeypatch.setattr(service, "search_soundcloud_profile_urls", lambda *args, **kwargs: list(profiles))
    monkeypatch.setattr(
        service,
        "fetch_soundcloud_profile",
        lambda url, **kwargs: profiles.get(url),
    )

    resolved = service.resolve_canonical_soundcloud_profile("Kai Angel")

    assert resolved is not None
    assert resolved.permalink_url == "https://soundcloud.com/4ngelkai"


def test_resolver_uses_verified_then_followers_for_equal_name_matches(monkeypatch) -> None:
    profiles = {
        "https://soundcloud.com/kishlak": _profile(
            "Кишлак☆",
            "https://soundcloud.com/kishlak",
            followers=105_366,
            verified=True,
        ),
        "https://soundcloud.com/kishlak-copy": _profile(
            "Кишлак",
            "https://soundcloud.com/kishlak-copy",
            followers=300_000,
            verified=False,
        ),
        "https://soundcloud.com/unrelated": _profile(
            "Unrelated uploader",
            "https://soundcloud.com/unrelated",
            followers=900_000,
            verified=True,
        ),
    }
    monkeypatch.setattr(service, "search_soundcloud_profile_urls", lambda *args, **kwargs: list(profiles))
    monkeypatch.setattr(
        service,
        "fetch_soundcloud_profile",
        lambda url, **kwargs: profiles.get(url),
    )

    resolved = service.resolve_canonical_soundcloud_profile("Кишлак")

    assert resolved is not None
    assert resolved.permalink_url == "https://soundcloud.com/kishlak"


def test_resolver_keeps_known_profile_when_search_fills_candidate_limit(monkeypatch) -> None:
    official_url = "https://soundcloud.com/4ngelkai"
    weak_url = "https://soundcloud.com/kai-angel-580741983"
    search_urls = [weak_url, *(f"https://soundcloud.com/unrelated-{index}" for index in range(11))]
    profiles = {
        weak_url: _profile("Kai Angel", weak_url, followers=0),
        official_url: _profile("Kai Angel", official_url, followers=38_602),
    }
    monkeypatch.setattr(service, "search_soundcloud_profile_urls", lambda *args, **kwargs: search_urls)
    monkeypatch.setattr(
        service,
        "fetch_soundcloud_profile",
        lambda url, **kwargs: profiles.get(url),
    )

    resolved = service.resolve_canonical_soundcloud_profile(
        "Kai Angel",
        [official_url],
        include_search=True,
        max_candidates=12,
    )

    assert resolved is not None
    assert resolved.permalink_url == official_url


def test_resolver_does_not_give_direct_legacy_url_ranking_priority(monkeypatch) -> None:
    official_url = "https://soundcloud.com/4ngelkai"
    legacy_url = "https://soundcloud.com/kai-angel-580741983"
    profiles = {
        official_url: _profile("Kai Angel", official_url, followers=38_602),
        legacy_url: _profile("Kai Angel", legacy_url, followers=0),
    }
    monkeypatch.setattr(service, "search_soundcloud_profile_urls", lambda *args, **kwargs: [official_url])
    monkeypatch.setattr(
        service,
        "fetch_soundcloud_profile",
        lambda url, **kwargs: profiles.get(url),
    )

    resolved = service.resolve_canonical_soundcloud_profile(
        "Kai Angel",
        [legacy_url],
        include_search=True,
    )

    assert resolved is not None
    assert resolved.permalink_url == official_url


def test_ytdlp_search_is_bounded_and_returns_unique_profile_urls(monkeypatch) -> None:
    captured: dict = {}
    payload = {
        "entries": [
            {
                "uploader_url": "https://soundcloud.com/kaiangel-9mice",
                "webpage_url": "https://soundcloud.com/kaiangel-9mice/song-a",
            },
            {"uploader_url": "https://soundcloud.com/kaiangel-9mice"},
            {"webpage_url": "https://soundcloud.com/4ngelkai/song-b"},
        ]
    }

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))

    monkeypatch.setattr(service.subprocess, "run", fake_run)

    urls = service.search_soundcloud_profile_urls("Kai Angel", limit=999, timeout=999)

    assert urls == [
        "https://soundcloud.com/kaiangel-9mice",
        "https://soundcloud.com/4ngelkai",
    ]
    assert "scsearch20:Kai Angel" in captured["command"]
    assert captured["kwargs"]["timeout"] == 20.0
    assert captured["kwargs"]["check"] is False
