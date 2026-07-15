import os

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.track import TrackArtist
from app.services import search_service
from app.services.normalization_service import normalize_artist_name
from app.services.soundcloud_profile_service import SoundCloudProfile


PROVIDER = {
    "name": "soundcloud",
    "tag": "soundcloud",
    "popularity_score": 75.0,
}


def _profile(username: str, slug: str, profile_id: str) -> SoundCloudProfile:
    return SoundCloudProfile(
        id=profile_id,
        urn=f"soundcloud:users:{profile_id}",
        username=username,
        permalink_url=f"https://soundcloud.com/{slug}",
        avatar_url=f"https://i1.sndcdn.com/avatars-{slug}-t500x500.jpg",
        followers_count=1234,
        track_count=10,
    )


def _result(*, title: str, uploader: str, slug: str, profile_id: str) -> dict:
    return {
        "id": f"track-{profile_id}",
        "title": title,
        "uploader": uploader,
        "uploader_id": profile_id,
        "uploader_url": f"https://soundcloud.com/{slug}",
        "webpage_url": f"https://soundcloud.com/{slug}/track-{profile_id}",
        "duration": 120,
        "thumbnail": "https://i1.sndcdn.com/artworks-track-t500x500.jpg",
    }


def test_reupload_credit_does_not_inherit_uploader_profile(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        search_service,
        "fetch_soundcloud_profile",
        lambda *_args, **_kwargs: _profile("vvender", "vvender", "777"),
    )

    with Session(engine) as db:
        assert search_service._save_provider_entry(
            db,
            "Kai Angel",
            PROVIDER,
            _result(
                title="Kai Angel, 9mice - test song",
                uploader="vvender",
                slug="vvender",
                profile_id="777",
            ),
        )

        artists = {
            artist.normalized_name: artist
            for artist in db.execute(select(Artist)).scalars().all()
        }
        credit = artists[normalize_artist_name("Kai Angel, 9mice")]
        uploader = artists[normalize_artist_name("vvender")]
        assert credit.is_canonical is False
        assert credit.avatar_url is None
        assert credit.source_url is None
        assert uploader.is_canonical is True
        assert uploader.avatar_url == "https://i1.sndcdn.com/avatars-vvender-t500x500.jpg"
        assert db.get(
            TrackArtist,
            {"track_id": 1, "artist_id": uploader.id, "role": "uploader"},
        ) is not None
    engine.dispose()


def test_exact_uploader_enriches_existing_seed_artist(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        search_service,
        "fetch_soundcloud_profile",
        lambda *_args, **_kwargs: _profile("kai angel", "4ngelkai", "1043731018"),
    )

    with Session(engine) as db:
        seed = Artist(
            name="Kai Angel",
            normalized_name=normalize_artist_name("Kai Angel"),
            region="global",
            genres_json="[]",
            source_name="artist_seed",
            seed_source="artist_seed",
            priority="high",
        )
        db.add(seed)
        db.commit()

        assert search_service._save_provider_entry(
            db,
            "Kai Angel",
            PROVIDER,
            _result(
                title="101",
                uploader="kai angel",
                slug="4ngelkai",
                profile_id="1043731018",
            ),
        )
        db.refresh(seed)
        assert seed.is_canonical is True
        assert seed.source_url == "https://soundcloud.com/4ngelkai"
        assert seed.source_external_id == "soundcloud:users:1043731018"
        assert seed.avatar_url == "https://i1.sndcdn.com/avatars-4ngelkai-t500x500.jpg"
        assert seed.source_followers_count == 1234
    engine.dispose()
