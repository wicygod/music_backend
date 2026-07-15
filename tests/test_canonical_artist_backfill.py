from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models  # noqa: F401 - register every mapped table before create_all
from app.database import Base
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.soundcloud_profile_service import SoundCloudProfile
from scripts.backfill_canonical_artists import (
    backfill_canonical_artists,
    collect_candidate_profile_urls,
    soundcloud_profile_root,
)


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _artist(db: Session, name: str, *, seeded: bool = True, **overrides) -> Artist:
    values = {
        "name": name,
        "normalized_name": normalize_artist_name(name),
        "region": "global",
        "genres_json": "[]",
        "source_name": "artist_seed" if seeded else "soundcloud",
        "source_external_id": f"artist_seed:{normalize_artist_name(name)}" if seeded else "dynamic:1",
        "source_url": None,
        "seed_source": "artist_seed" if seeded else None,
        "import_status": "imported",
        "priority": "high" if seeded else "normal",
        "tracks_target": 60 if seeded else 25,
        "needs_review": False,
    }
    values.update(overrides)
    artist = Artist(**values)
    db.add(artist)
    db.flush()
    return artist


def _track(db: Session, artist: Artist, source_url: str, *, title: str) -> Track:
    track = Track(
        title=title,
        normalized_title=normalize_title(title),
        duration_seconds=120,
        cover_url=None,
        genre="Hip-Hop",
        tags_json="[]",
        region="global",
        popularity_score=75,
        quality_score=100,
        is_playable=True,
        source_name="soundcloud",
        source_external_id=f"track:{title}",
        source_url=source_url,
        needs_review=False,
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    db.flush()
    return track


def _profile(
    username: str,
    profile_url: str,
    *,
    avatar_url: str = "https://i1.sndcdn.com/avatars-test-t500x500.jpg",
    followers: int = 10,
    verified: bool = False,
) -> SoundCloudProfile:
    return SoundCloudProfile(
        id="1043731018",
        urn="soundcloud:users:1043731018",
        username=username,
        permalink_url=profile_url,
        avatar_url=avatar_url,
        followers_count=followers,
        verified=verified,
        track_count=20,
    )


def test_soundcloud_profile_root_accepts_profiles_and_tracks_only() -> None:
    assert soundcloud_profile_root("http://www.soundcloud.com/4ngelkai/song?x=1") == (
        "https://soundcloud.com/4ngelkai"
    )
    assert soundcloud_profile_root("https://soundcloud.com/kishlak") == "https://soundcloud.com/kishlak"
    assert soundcloud_profile_root("https://soundcloud.com/search?q=kai") is None
    assert soundcloud_profile_root("https://i1.sndcdn.com/avatars-test.jpg") is None
    assert soundcloud_profile_root("https://example.com/soundcloud.com/kai") is None


def test_collect_candidate_urls_uses_artist_metadata_and_linked_track_roots() -> None:
    engine = _engine()
    with Session(engine) as db:
        artist = _artist(
            db,
            "Candidate Artist",
            source_url="https://soundcloud.com/source-profile/sets/all",
            avatar_url="https://soundcloud.com/avatar-profile",
        )
        _track(db, artist, "https://soundcloud.com/track-profile/song-one", title="One")
        _track(db, artist, "https://soundcloud.com/source-profile/song-two", title="Two")
        db.commit()

        db.refresh(artist)
        assert collect_candidate_profile_urls(artist) == [
            "https://soundcloud.com/source-profile",
            "https://soundcloud.com/avatar-profile",
            "https://soundcloud.com/track-profile",
        ]
    engine.dispose()


def test_backfill_enriches_exact_seed_profile_without_merging_rows() -> None:
    engine = _engine()
    calls: list[dict] = []
    resolved_at = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)

    def resolver(query, candidates, **kwargs):
        calls.append({"query": query, "candidates": list(candidates), **kwargs})
        return _profile(
            "Kai Angel",
            "https://soundcloud.com/4ngelkai",
            avatar_url="https://i1.sndcdn.com/avatars-kai-t500x500.jpg",
            followers=38_602,
            verified=True,
        )

    with Session(engine) as db:
        seed = _artist(db, "Kai Angel")
        dynamic = _artist(db, "Kai Angel, 9mice", seeded=False)
        _track(db, seed, "https://soundcloud.com/4ngelkai/101-prichina", title="101")
        _track(db, dynamic, "https://soundcloud.com/vvender/reupload", title="Reupload")
        seed_id = seed.id
        dynamic_id = dynamic.id
        db.commit()

        summary = backfill_canonical_artists(
            db,
            limit=20,
            resolver=resolver,
            resolved_at=resolved_at,
        )

    assert summary.scanned == 1
    assert summary.resolved == 1
    assert summary.updated == 1
    assert summary.failed == 0
    assert calls == [
        {
            "query": "Kai Angel",
            "candidates": ["https://soundcloud.com/4ngelkai"],
            "include_search": True,
            "max_candidates": 12,
            "timeout": 12.0,
        },
    ]

    with Session(engine) as db:
        seed = db.get(Artist, seed_id)
        dynamic = db.get(Artist, dynamic_id)
        assert seed is not None
        assert seed.source_name == "soundcloud"
        assert seed.source_external_id == "soundcloud:users:1043731018"
        assert seed.source_url == "https://soundcloud.com/4ngelkai"
        assert seed.avatar_url == "https://i1.sndcdn.com/avatars-kai-t500x500.jpg"
        assert seed.source_followers_count == 38_602
        assert seed.source_verified is True
        assert seed.is_canonical is True
        assert seed.profile_resolved_at == datetime(2026, 7, 15, 12, 30)
        assert seed.seed_source == "artist_seed"
        assert dynamic is not None
        assert dynamic.is_canonical is False
        assert dynamic.source_url is None
    engine.dispose()


def test_backfill_rejects_compound_identity_for_exact_seed_artist() -> None:
    engine = _engine()

    def resolver(_query, _candidates, **_kwargs):
        return _profile("Kai Angel & 9mice", "https://soundcloud.com/kaiangel-9mice")

    with Session(engine) as db:
        artist = _artist(db, "Kai Angel")
        artist_id = artist.id
        db.commit()
        summary = backfill_canonical_artists(db, resolver=resolver)

    assert summary.resolved == 1
    assert summary.updated == 0
    assert summary.skipped_non_exact == 1
    with Session(engine) as db:
        artist = db.get(Artist, artist_id)
        assert artist is not None
        assert artist.source_name == "artist_seed"
        assert artist.is_canonical is False
        assert artist.profile_resolved_at is None
    engine.dispose()


def test_backfill_uses_bounded_search_without_urls_and_dry_run_rolls_back() -> None:
    engine = _engine()
    calls: list[dict] = []

    def resolver(query, candidates, **kwargs):
        calls.append({"query": query, "candidates": list(candidates), **kwargs})
        return _profile(
            "Кишлак☆",
            "https://soundcloud.com/kishlak",
            avatar_url="https://i1.sndcdn.com/avatars-kishlak-t500x500.jpg",
            followers=105_000,
            verified=True,
        )

    with Session(engine) as db:
        artist = _artist(db, "Кишлак")
        artist_id = artist.id
        db.commit()
        summary = backfill_canonical_artists(
            db,
            query="Кишлак",
            limit=1,
            dry_run=True,
            resolver=resolver,
        )

    assert summary.dry_run is True
    assert summary.updated == 1
    assert calls[0]["candidates"] == []
    assert calls[0]["include_search"] is True
    assert calls[0]["max_candidates"] == 12
    with Session(engine) as db:
        artist = db.get(Artist, artist_id)
        assert artist is not None
        assert artist.source_name == "artist_seed"
        assert artist.avatar_url is None
        assert artist.is_canonical is False
    engine.dispose()
