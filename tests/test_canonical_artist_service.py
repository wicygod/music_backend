from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.services import canonical_artist_service as service
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.soundcloud_profile_service import SoundCloudProfile


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _artist(db: Session, name: str, *, seed: bool, artist_id_suffix: str) -> Artist:
    artist = Artist(
        name=name,
        normalized_name=normalize_artist_name(name),
        region="global",
        genres_json="[]",
        priority="high" if seed else "normal",
        seed_source="artist_seed" if seed else None,
        source_name="artist_seed" if seed else "soundcloud",
        source_external_id=f"old:{artist_id_suffix}",
        is_canonical=False,
    )
    db.add(artist)
    db.flush()
    track = Track(
        title=f"{name} track {artist_id_suffix}",
        normalized_title=normalize_title(f"{name} track {artist_id_suffix}"),
        duration_seconds=120,
        genre="trap",
        popularity_score=75,
        quality_score=100,
        is_playable=True,
        source_name="soundcloud",
        source_url=f"https://soundcloud.com/{artist_id_suffix}/song",
        needs_review=False,
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    return artist


def test_exact_search_refreshes_seed_identity_and_demotes_duplicate(monkeypatch) -> None:
    engine = _engine()
    calls: list[tuple] = []
    profile = SoundCloudProfile(
        id="1043731018",
        urn="soundcloud:users:1043731018",
        username="kai angel",
        permalink_url="https://soundcloud.com/4ngelkai",
        avatar_url="https://i1.sndcdn.com/avatars-kai-t500x500.jpg",
        followers_count=38_602,
        track_count=229,
    )

    def resolver(*args, **kwargs):
        calls.append((args, kwargs))
        return profile

    monkeypatch.setattr(service, "resolve_canonical_soundcloud_profile", resolver)
    with Session(engine) as db:
        seed = _artist(db, "Kai Angel", seed=True, artist_id_suffix="4ngelkai")
        duplicate = _artist(db, "Kai Angel", seed=False, artist_id_suffix="fan-uploader")
        db.commit()

        resolved = service.refresh_canonical_artist_for_search(db, "  KAI   ANGEL  ")
        db.commit()

        assert resolved is not None
        assert resolved.id == seed.id
        assert resolved.source_external_id == "soundcloud:users:1043731018"
        assert resolved.avatar_url == "https://i1.sndcdn.com/avatars-kai-t500x500.jpg"
        assert resolved.source_followers_count == 38_602
        assert resolved.is_canonical is True
        assert db.get(Artist, duplicate.id).is_canonical is False

    assert calls
    assert calls[0][1]["include_search"] is True
    assert "https://soundcloud.com/4ngelkai" in calls[0][0][1]
    engine.dispose()


def test_fresh_profile_and_partial_query_do_not_call_provider(monkeypatch) -> None:
    engine = _engine()
    calls = 0

    def resolver(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(service, "resolve_canonical_soundcloud_profile", resolver)
    with Session(engine) as db:
        artist = _artist(db, "Кишлак", seed=True, artist_id_suffix="kishlak")
        artist.avatar_url = "https://i1.sndcdn.com/avatars-kishlak-t500x500.jpg"
        artist.source_url = "https://soundcloud.com/kishlak"
        artist.source_followers_count = 105_367
        artist.source_verified = True
        artist.is_canonical = True
        artist.profile_resolved_at = datetime.utcnow()
        db.commit()

        assert service.refresh_canonical_artist_for_search(db, "киш") is None
        assert service.refresh_canonical_artist_for_search(db, "Кишлак") is not None
        assert calls == 0
    engine.dispose()
