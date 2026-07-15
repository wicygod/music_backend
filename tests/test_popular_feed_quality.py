import os
from datetime import datetime, timedelta

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.track import Track, TrackArtist
from app.repositories.tracks import list_trending_rankings
from app.services.feed_service import get_home_feed
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.popular_ranking_service import POPULAR_ALGORITHM_VERSION, PROVIDER_POPULARITY_TAG
from app.services.provider_popularity_refresh_service import refresh_provider_popularity


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _artist_with_track(
    db: Session,
    artist_name: str,
    song_name: str,
    *,
    followers: int = 50_000,
    canonical: bool = True,
    verified: bool = False,
    popularity: float = 75.0,
    quality: float = 100.0,
    duration: int = 180,
    title_artist: str | None = None,
    created_at: datetime | None = None,
) -> tuple[Artist, Track]:
    slug = normalize_artist_name(artist_name).replace(" ", "-")
    artist = Artist(
        name=artist_name,
        normalized_name=normalize_artist_name(artist_name),
        region="global",
        genres_json='["hip-hop"]',
        popularity_score=popularity,
        source_name="soundcloud",
        source_external_id=f"profile:{slug}",
        source_url=f"https://soundcloud.com/{slug}",
        source_followers_count=followers,
        source_verified=verified,
        is_canonical=canonical,
        needs_review=False,
    )
    db.add(artist)
    db.flush()
    display_artist = title_artist if title_artist is not None else artist_name
    title = f"{display_artist} - {song_name}"
    track = Track(
        title=title,
        normalized_title=normalize_title(title),
        duration_seconds=duration,
        genre="hip-hop",
        tags_json="[]",
        region="global",
        popularity_score=popularity,
        quality_score=quality,
        is_playable=True,
        source_name="soundcloud",
        source_external_id=f"track:{slug}:{normalize_title(song_name)}",
        source_url=f"https://soundcloud.com/{slug}/{normalize_title(song_name).replace(' ', '-')}",
        needs_review=False,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    db.flush()
    return artist, track


def _legacy_play(db: Session, track: Track, user_id: str, count: int = 1) -> None:
    db.add(
        ListeningHistory(
            user_id=user_id,
            track_id=track.id,
            event_id=None,
            play_count=count,
        )
    )


def _detailed_play(
    db: Session,
    track: Track,
    event_id: str,
    *,
    completed: bool = False,
    skipped: bool = False,
) -> None:
    db.add(
        ListeningHistory(
            user_id=f"event-user:{event_id}",
            track_id=track.id,
            event_id=event_id,
            play_count=1,
            listened_duration_seconds=175 if completed else 3,
            track_duration_seconds=180,
            completion_ratio=0.97 if completed else 0.02,
            completed=completed,
            skipped=skipped,
        )
    )


def test_repository_requires_canonical_quality_and_real_evidence() -> None:
    engine = _engine()
    with Session(engine) as db:
        _artist, accepted = _artist_with_track(db, "Major Artist", "Real Single")
        _artist, low_authority = _artist_with_track(
            db,
            "Tiny Profile",
            "Two Play Upload",
            followers=20,
        )
        _legacy_play(db, low_authority, "account:one", count=2)
        _artist_with_track(
            db,
            "Unverified Reuploader",
            "Fake Viral Upload",
            followers=1_000_000,
            canonical=False,
            verified=True,
            popularity=91.0,
        )
        _artist_with_track(db, "Low Quality Artist", "Broken File", quality=69.0)
        db.commit()

        rankings = list_trending_rankings(db, limit=20, rotation_key="test")

        assert [item.item.id for item in rankings] == [accepted.id]

    engine.dispose()


def test_explicit_unknown_artist_cannot_borrow_a_canonical_uploader() -> None:
    engine = _engine()
    with Session(engine) as db:
        _uploader, unrelated = _artist_with_track(
            db,
            "Russian Rap Scene",
            "Leaked Song",
            followers=500_000,
            verified=True,
            popularity=90.0,
            title_artist="MORGENSHTERN",
        )
        db.commit()

        rankings = list_trending_rankings(db, limit=20, rotation_key="test")

        assert unrelated.id not in {item.item.id for item in rankings}

    engine.dispose()


def test_unique_listeners_beat_one_account_repeating_a_track() -> None:
    engine = _engine()
    with Session(engine) as db:
        _artist, repeated = _artist_with_track(db, "Repeat Artist", "Looped")
        _artist, community = _artist_with_track(db, "Community Artist", "Shared")
        _legacy_play(db, repeated, "account:repeat", count=100)
        for index in range(5):
            _legacy_play(db, community, f"account:{index}")
        db.commit()

        rankings = list_trending_rankings(db, limit=10, rotation_key="engagement")
        by_id = {item.item.id: item for item in rankings}

        assert by_id[repeated.id].capped_plays == 5
        assert by_id[repeated.id].unique_listeners == 1
        assert by_id[community.id].capped_plays == 5
        assert by_id[community.id].unique_listeners == 5
        assert rankings[0].item.id == community.id

    engine.dispose()


def test_completed_events_beat_quick_skips_with_equal_reach() -> None:
    engine = _engine()
    with Session(engine) as db:
        _artist, completed = _artist_with_track(db, "Finish Artist", "Played Through")
        _artist, skipped = _artist_with_track(db, "Skip Artist", "Skipped Fast")
        for index in range(3):
            _legacy_play(db, completed, f"completed-listener:{index}")
            _legacy_play(db, skipped, f"skipped-listener:{index}")
        for index in range(5):
            _detailed_play(db, completed, f"completed:{index}", completed=True)
            _detailed_play(db, skipped, f"skipped:{index}", skipped=True)
        db.commit()

        rankings = list_trending_rankings(db, limit=10, rotation_key="completion")
        by_id = {item.item.id: item for item in rankings}

        assert by_id[completed.id].completed_plays == 5
        assert by_id[skipped.id].skipped_plays == 5
        assert rankings[0].item.id == completed.id

    engine.dispose()


def test_rejected_variant_does_not_hide_the_clean_original() -> None:
    engine = _engine()
    with Session(engine) as db:
        earlier = datetime.utcnow() - timedelta(minutes=5)
        _artist, clean = _artist_with_track(
            db,
            "Signal Artist",
            "Original",
            created_at=earlier,
        )
        # The newer variant is scanned first and normalizes to the same song key.
        _artist, variant = _artist_with_track(
            db,
            "Signal Artist",
            "Original (slowed + reverb)",
            created_at=datetime.utcnow(),
        )
        db.commit()

        rankings = list_trending_rankings(db, limit=10, rotation_key="variants")
        ids = {item.item.id for item in rankings}

        assert variant.id not in ids
        assert clean.id in ids

    engine.dispose()


def test_home_feed_exposes_authoritative_top_contract() -> None:
    engine = _engine()
    with Session(engine) as db:
        _artist, track = _artist_with_track(db, "Feed Artist", "Chart Song")
        db.commit()

        feed = get_home_feed(db, user_id="local")

        assert [item.id for item in feed.top] == [track.id]
        assert [item.id for item in feed.trending] == [track.id]
        assert feed.popular_algorithm_version == POPULAR_ALGORITHM_VERSION
        assert feed.popular_window_days == 14

    engine.dispose()


def test_provider_popularity_refresh_supports_dry_run_and_live_update() -> None:
    engine = _engine()
    payload = {"view_count": 3_600_000, "like_count": 56_000, "repost_count": 235}
    with Session(engine) as db:
        _artist, track = _artist_with_track(db, "Refresh Artist", "Legacy Score")
        db.commit()
        original_score = track.popularity_score

        dry_result = refresh_provider_popularity(
            db,
            limit=10,
            dry_run=True,
            extractor=lambda _url: payload,
        )
        persisted_after_dry_run = db.scalar(select(Track.popularity_score).where(Track.id == track.id))

        assert dry_result.scanned == dry_result.updated == 1
        assert dry_result.dry_run is True
        assert persisted_after_dry_run == original_score

        live_result = refresh_provider_popularity(
            db,
            limit=10,
            dry_run=False,
            extractor=lambda _url: payload,
        )
        persisted_after_live_run = db.scalar(select(Track.popularity_score).where(Track.id == track.id))
        persisted_tags = db.scalar(select(Track.tags_json).where(Track.id == track.id))

        assert live_result.scanned == live_result.updated == 1
        assert live_result.dry_run is False
        assert live_result.last_track_id == track.id
        assert persisted_after_live_run != original_score
        assert 0 < persisted_after_live_run <= 100
        assert PROVIDER_POPULARITY_TAG in str(persisted_tags)

    engine.dispose()


def test_provider_popularity_refresh_can_target_chart_candidates() -> None:
    engine = _engine()
    payload = {"view_count": 900_000, "like_count": 12_000, "repost_count": 90}
    with Session(engine) as db:
        _artist, outside = _artist_with_track(db, "Outside Artist", "Outside")
        _artist, target = _artist_with_track(db, "Target Artist", "Target")
        db.commit()
        original_outside = outside.popularity_score
        original_target = target.popularity_score

        result = refresh_provider_popularity(
            db,
            limit=10,
            dry_run=False,
            extractor=lambda _url: payload,
            track_ids=[target.id],
        )

        assert result.scanned == result.updated == 1
        assert db.scalar(select(Track.popularity_score).where(Track.id == outside.id)) == original_outside
        assert db.scalar(select(Track.popularity_score).where(Track.id == target.id)) != original_target

    engine.dispose()
