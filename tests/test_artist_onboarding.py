import os

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.repositories.personalization import list_onboarding_artists
from app.services.normalization_service import normalize_artist_name, normalize_title


def _artist_with_track(
    db: Session,
    name: str,
    genre: str,
    *,
    playable: bool = True,
    quality: float = 100.0,
    track_needs_review: bool = False,
    artist_needs_review: bool = False,
    avatar_url: str | None = None,
    is_canonical: bool = True,
    source_followers_count: int = 1_000,
    source_verified: bool = False,
) -> tuple[Artist, Track]:
    slug = normalize_artist_name(name).replace(" ", "-")
    artist = Artist(
        name=name,
        normalized_name=normalize_artist_name(name),
        avatar_url=avatar_url,
        region="global",
        genres_json="[]",
        priority="normal",
        needs_review=artist_needs_review,
        source_name="soundcloud",
        source_external_id=f"profile:{slug}",
        source_url=f"https://soundcloud.com/{slug}",
        source_followers_count=source_followers_count,
        source_verified=source_verified,
        is_canonical=is_canonical,
    )
    db.add(artist)
    db.flush()
    track = Track(
        title=f"{name} song",
        normalized_title=normalize_title(f"{name} song"),
        duration_seconds=180,
        cover_url=f"https://images.example/{slug}.jpg",
        genre=genre,
        popularity_score=60.0,
        quality_score=quality,
        is_playable=playable,
        source_name="soundcloud",
        source_url=f"https://soundcloud.com/test/{slug}",
        needs_review=track_needs_review,
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    return artist, track


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_onboarding_search_pagination_genre_and_selected_state_are_stable() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="onboarding-user", nickname="Listener", password_hash="hash")
        db.add(user)
        artists = [
            _artist_with_track(db, "Neon River", "Rock"),
            _artist_with_track(db, "Neon Lake", "Pop"),
            _artist_with_track(db, "Amber Field", "Trap"),
            _artist_with_track(db, "Blue Static", "Rock"),
            _artist_with_track(db, "Quiet Frame", "Ambient"),
            _artist_with_track(db, "Silver Wire", "Electronic"),
        ]
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=artists[0][0].id,
                source="onboarding",
                explicit_weight=5.0,
                weight=5.0,
                explicit_selected=True,
            )
        )
        db.add(
            ListeningHistory(
                user_id=f"account:{user.id}",
                track_id=artists[0][1].id,
                event_id=None,
                play_count=12,
            )
        )
        db.commit()

        first, total = list_onboarding_artists(db, user_id=user.id, page=1, limit=2)
        second, second_total = list_onboarding_artists(db, user_id=user.id, page=2, limit=2)
        repeated_first, _ = list_onboarding_artists(db, user_id=user.id, page=1, limit=2)

        assert total == second_total == 6
        assert [item.id for item in first] == [item.id for item in repeated_first]
        assert set(item.id for item in first).isdisjoint(item.id for item in second)

        search, search_total = list_onboarding_artists(
            db,
            user_id=user.id,
            search="Neon",
            page=1,
            limit=10,
        )
        assert search_total == 1
        assert [item.name for item in search] == ["Neon River"]
        assert search[0].selected is True

        rock, rock_total = list_onboarding_artists(
            db,
            user_id=user.id,
            genre="rock",
            page=1,
            limit=10,
        )
        assert rock_total == 2
        assert all("rock" in item.genres for item in rock)

    engine.dispose()


def test_onboarding_excludes_unavailable_low_quality_and_review_items() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="eligibility-user", nickname="Listener", password_hash="hash")
        db.add(user)
        eligible_artist, _eligible_track = _artist_with_track(
            db,
            "Available Artist",
            "Rock",
            avatar_url="https://i1.sndcdn.com/avatars-available-t500x500.jpg",
        )
        no_avatar_artist, no_avatar_track = _artist_with_track(db, "No Avatar Artist", "Pop")
        _artist_with_track(db, "Unavailable Artist", "Rock", playable=False)
        _artist_with_track(db, "Low Quality Artist", "Rock", quality=59.9)
        _artist_with_track(db, "Review Track Artist", "Rock", track_needs_review=True)
        _artist_with_track(db, "Review Artist", "Rock", artist_needs_review=True)
        _artist_with_track(db, "Noncanonical Artist", "Rock", is_canonical=False)
        db.commit()

        items, total = list_onboarding_artists(db, user_id=user.id, page=1, limit=20)

        assert total == 2
        assert {item.id for item in items} == {eligible_artist.id, no_avatar_artist.id}
        eligible_item = next(item for item in items if item.id == eligible_artist.id)
        assert eligible_item.track_count == 1
        assert eligible_item.avatar_url == "https://i1.sndcdn.com/avatars-available-t500x500.jpg"
        assert eligible_item.genres == ["rock"]
        no_avatar_item = next(item for item in items if item.id == no_avatar_artist.id)
        assert no_avatar_item.avatar_url is None
        assert no_avatar_item.avatar_url != no_avatar_track.cover_url

    engine.dispose()


def test_onboarding_search_returns_only_the_best_exact_canonical_profile() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="canonical-search-user", nickname="Listener", password_hash="hash")
        db.add(user)
        solo, _ = _artist_with_track(
            db,
            "Kai Angel",
            "Trap",
            avatar_url="https://i1.sndcdn.com/avatars-kai-solo-t500x500.jpg",
            source_followers_count=38_602,
        )
        _artist_with_track(
            db,
            "Kai Angel & 9mice",
            "Trap",
            avatar_url="https://i1.sndcdn.com/avatars-kai-duo-t500x500.jpg",
            source_followers_count=84_460,
            source_verified=True,
        )
        _artist_with_track(
            db,
            "Kai Angel",
            "Trap",
            avatar_url="https://i1.sndcdn.com/avatars-kai-fan-t500x500.jpg",
            source_followers_count=1_696,
        )
        _artist_with_track(
            db,
            "Kai Angel",
            "Trap",
            avatar_url="https://i1.sndcdn.com/avatars-kai-noncanonical-t500x500.jpg",
            source_followers_count=1_000_000,
            source_verified=True,
            is_canonical=False,
        )
        db.commit()

        items, total = list_onboarding_artists(
            db,
            user_id=user.id,
            search="kai angel",
            page=1,
            limit=24,
        )
        next_page, next_total = list_onboarding_artists(
            db,
            user_id=user.id,
            search="kai angel",
            page=2,
            limit=24,
        )

        assert total == next_total == 1
        assert [item.id for item in items] == [solo.id]
        assert items[0].avatar_url == "https://i1.sndcdn.com/avatars-kai-solo-t500x500.jpg"
        assert items[0].popularity_score == 38_602
        assert next_page == []

        default_items, default_total = list_onboarding_artists(
            db,
            user_id=user.id,
            page=1,
            limit=24,
        )
        assert default_total == 2
        assert [item.name for item in default_items].count("Kai Angel") == 1

    engine.dispose()
