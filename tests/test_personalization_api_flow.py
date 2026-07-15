import os

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.routers import artists, auth, feed, history, personalization
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.recommendation_service import invalidate_recommendations


def _catalog_artist(db: Session, index: int) -> tuple[Artist, Track]:
    artist = Artist(
        name=f"Flow Artist {index}",
        normalized_name=normalize_artist_name(f"Flow Artist {index}"),
        region="global",
        genres_json="[]",
        priority="normal",
        avatar_url=f"https://i1.sndcdn.com/avatars-flow-{index}-t500x500.jpg",
        source_name="soundcloud",
        source_external_id=f"soundcloud:users:flow-{index}",
        source_url=f"https://soundcloud.com/flow-artist-{index}",
        source_followers_count=10_000 - index,
        is_canonical=True,
    )
    db.add(artist)
    db.flush()
    track = Track(
        title=f"Flow Artist {index} - Song {index}",
        normalized_title=normalize_title(f"Flow Artist {index} - Song {index}"),
        duration_seconds=100,
        genre=("rock", "pop", "electronic")[index - 1],
        popularity_score=70 - index,
        quality_score=95,
        is_playable=True,
        source_name="soundcloud",
        source_url=f"https://soundcloud.com/flow-artist-{index}/song-{index}",
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    return artist, track


def test_registration_onboarding_feed_and_listening_event_work_as_one_api_flow() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(engine)
    # Keep seeded IDs available after the short-lived setup session closes.
    # The API itself still uses the normal expiring session factory below.
    with Session(engine, expire_on_commit=False) as db:
        catalog = [_catalog_artist(db, index) for index in range(1, 4)]
        db.commit()

    api = FastAPI()

    @api.middleware("http")
    async def trusted_test_user(request: Request, call_next):
        raw_user_id = request.headers.get("X-Test-User")
        if raw_user_id:
            request.state.user_id = int(raw_user_id)
        return await call_next(request)

    def test_db():
        with session_factory() as db:
            yield db

    api.dependency_overrides[get_db] = test_db
    api.include_router(auth.router)
    api.include_router(artists.router)
    api.include_router(personalization.router)
    api.include_router(feed.router)
    api.include_router(history.router)
    invalidate_recommendations()

    with TestClient(api) as client:
        registration = client.post(
            "/api/auth/register",
            json={"login": "flow-user", "nickname": "Flow User", "password": "secret42"},
        )
        assert registration.status_code == 200
        user = registration.json()["user"]
        assert user["music_preferences_completed_at"] is None
        headers = {"X-Test-User": str(user["id"])}

        onboarding = client.get("/api/artists/onboarding?page=1&limit=24", headers=headers)
        assert onboarding.status_code == 200
        artist_ids = [item["id"] for item in onboarding.json()["items"]]
        assert len(artist_ids) == 3

        saved = client.post(
            "/api/user/music-preferences",
            headers=headers,
            json={"artistIds": artist_ids + [artist_ids[0]], "source": "onboarding"},
        )
        assert saved.status_code == 200
        assert sorted(saved.json()["selectedArtistIds"]) == sorted(artist_ids)
        assert saved.json()["completedAt"]

        home = client.get("/api/feed/home", headers=headers)
        assert home.status_code == 200
        assert home.json()["personalization_active"] is True
        assert home.json()["algorithm_version"] == "personalized-v2"
        assert home.json()["personalized"]

        track_id = catalog[0][1].id
        assert client.post(f"/api/history/listen/{track_id}", headers=headers).status_code == 200
        detailed = client.post(
            "/api/history/events",
            headers=headers,
            json={
                "eventId": "flow-event-0001",
                "trackId": track_id,
                "artistId": catalog[0][0].id,
                "startedAt": "2026-07-15T10:00:00Z",
                "listenedDuration": 80,
                "trackDuration": 100,
                "completionRatio": 0.8,
                "completed": False,
                "skipped": False,
                "context": "home",
                "recommendationType": "selected",
                "recommendationReason": "From onboarding",
                "algorithmVersion": "personalized-v2",
            },
        )
        assert detailed.status_code == 200
        assert detailed.json()["completionRatio"] == 0.8
        duplicate = client.post(
            "/api/history/events",
            headers=headers,
            json={
                "eventId": "flow-event-0001",
                "trackId": track_id,
                "artistId": catalog[0][0].id,
                "startedAt": "2026-07-15T10:00:00Z",
                "listenedDuration": 80,
                "trackDuration": 100,
                "completionRatio": 0.8,
                "context": "home",
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == detailed.json()["id"]

    invalidate_recommendations()
    engine.dispose()
