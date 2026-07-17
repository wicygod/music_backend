import json
import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from fastapi import BackgroundTasks
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.repositories.albums import link_album_track, search_album_matches, upsert_album
from app.routers import search as search_router
from app.schemas.album import SearchOverview
from app.services import search_service
from app.services.normalization_service import normalize_name, normalize_title
from app.services.serialization_service import album_to_read


def _artist(db: Session, name: str, *, source_slug: str | None = None) -> Artist:
    artist = Artist(
        name=name,
        normalized_name=normalize_name(name),
        genres_json="[]",
        source_name="soundcloud" if source_slug else None,
        source_url=f"https://soundcloud.com/{source_slug}" if source_slug else None,
        is_canonical=bool(source_slug),
    )
    db.add(artist)
    db.flush()
    return artist


def _track(
    db: Session,
    *,
    title: str,
    artists: list[Artist],
    external_id: str,
    is_playable: bool = True,
) -> Track:
    track = Track(
        title=title,
        normalized_title=normalize_title(title),
        duration_seconds=180,
        tags_json="[]",
        region="global",
        popularity_score=25,
        quality_score=100,
        is_playable=is_playable,
        source_name="soundcloud",
        source_external_id=external_id,
        source_url=f"https://soundcloud.com/catalog/{external_id}",
        needs_review=False,
    )
    db.add(track)
    db.flush()
    db.add_all(
        TrackArtist(track_id=track.id, artist_id=artist.id, role="main")
        for artist in artists
    )
    return track


def test_punctuation_only_search_is_empty_and_does_not_schedule_hydration(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled: list[str] = []
    monkeypatch.setattr(
        search_service,
        "_schedule_hydration",
        lambda query, *_args, **_kwargs: scheduled.append(query),
    )

    with Session(engine) as db:
        assert search_service.search_local_catalog(db, "---", limit=20) == []
        assert search_service.search_local_catalog(db, "... ///", limit=20) == []

    assert scheduled == []
    engine.dispose()


def test_local_catalog_finds_clams_casino_through_claims_typo(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(search_service, "_schedule_hydration", lambda *_args, **_kwargs: None)

    with Session(engine) as db:
        artist = _artist(db, "Clams Casino", source_slug="clamscasino")
        expected = _track(
            db,
            title="All I Need",
            artists=[artist],
            external_id="clams-all-i-need",
        )
        db.commit()

        results = search_service.search_local_catalog(
            db,
            "claims casino all i need",
            limit=20,
        )

    assert [track.id for track in results] == [expected.id]
    engine.dispose()


def test_punctuation_agnostic_artist_and_title_queries(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(search_service, "_schedule_hydration", lambda *_args, **_kwargs: None)

    with Session(engine) as db:
        mia = _artist(db, "M.I.A.", source_slug="mia")
        paper_planes = _track(
            db,
            title="Paper Planes",
            artists=[mia],
            external_id="paper-planes",
        )
        another_artist = _artist(db, "Example Artist", source_slug="example-artist")
        dont = _track(
            db,
            title="Don't",
            artists=[another_artist],
            external_id="dont",
        )
        db.commit()

        assert [item.id for item in search_service.search_local_catalog(db, "MIA")] == [
            paper_planes.id
        ]
        assert [item.id for item in search_service.search_local_catalog(db, "M.I.A.")] == [
            paper_planes.id
        ]
        assert [item.id for item in search_service.search_local_catalog(db, "dont")] == [
            dont.id
        ]
        assert [item.id for item in search_service.search_local_catalog(db, "Don't")] == [
            dont.id
        ]

    engine.dispose()


def test_same_title_by_different_artists_is_not_deduplicated(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(search_service, "_schedule_hydration", lambda *_args, **_kwargs: None)

    with Session(engine) as db:
        first_artist = _artist(db, "Clams Casino", source_slug="clamscasino")
        second_artist = _artist(db, "Other Artist", source_slug="other-artist")
        first = _track(
            db,
            title="All I Need",
            artists=[first_artist],
            external_id="all-i-need-clams",
        )
        second = _track(
            db,
            title="All I Need",
            artists=[second_artist],
            external_id="all-i-need-other",
        )
        db.commit()

        results = search_service.search_local_catalog(db, "All I Need", limit=20)

    assert {track.id for track in results} == {first.id, second.id}
    engine.dispose()


def test_authoritative_artist_prefixed_title_beats_fake_exact_title() -> None:
    fake = SimpleNamespace(
        title="All I Need",
        duration_seconds=180,
        popularity_score=500,
        quality_score=100,
        is_playable=True,
        source_name="soundcloud",
        source_url="https://soundcloud.com/all-i-need-reuploads/all-i-need",
        needs_review=False,
        artist_links=[
            SimpleNamespace(
                role="main",
                artist=SimpleNamespace(
                    name="All I Need",
                    is_canonical=False,
                    source_verified=False,
                    source_followers_count=0,
                ),
            )
        ],
    )
    original = SimpleNamespace(
        title="Clams Casino - All I Need",
        duration_seconds=204,
        popularity_score=100,
        quality_score=100,
        is_playable=True,
        source_name="soundcloud",
        source_url="https://soundcloud.com/clamscasino/all-i-need",
        needs_review=False,
        artist_links=[
            SimpleNamespace(
                role="main",
                artist=SimpleNamespace(
                    name="Clams Casino",
                    is_canonical=True,
                    source_verified=True,
                    source_followers_count=100_000,
                ),
            )
        ],
    )

    assert search_service._prefer_title_matches([fake, original], "All I Need")[0] is original


def test_malformed_provider_duration_is_rejected_without_exception() -> None:
    entry = {
        "id": "broken-duration",
        "title": "Example song",
        "artist": "Example Artist",
        "webpage_url": "https://soundcloud.com/example-artist/example-song",
        "duration": "N/A",
    }

    assert search_service._is_allowed_provider_entry("soundcloud", entry) is False


def test_id_only_youtube_entry_gets_a_canonical_music_url() -> None:
    entry = {
        "id": "dQw4w9WgXcQ",
        "title": "Example Artist - Example song",
        "artist": "Example Artist",
        "channel": "Example Artist - Topic",
        "categories": ["Music"],
        "duration": 180,
    }

    assert search_service._candidate_source_url("youtube", entry) == (
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert search_service._is_allowed_provider_entry("youtube", entry)


def test_provider_upsert_does_not_publish_user_queries_as_track_tags() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    provider = {
        "name": "youtube",
        "tag": "youtube_music",
        "popularity_score": 65.0,
    }
    entry = {
        "id": "private-query-track",
        "title": "Example song",
        "artist": "Example Artist",
        "channel": "Example Artist - Topic",
        "categories": ["Music"],
        "webpage_url": "https://music.youtube.com/watch?v=privateQuery",
        "duration": 180,
    }
    searches = ("private first search phrase", "another private search phrase")

    with Session(engine) as db:
        for query in searches:
            assert search_service._save_provider_entry(db, query, provider, entry)
        track = db.execute(select(Track)).scalars().one()
        tags = {str(tag) for tag in json.loads(track.tags_json)}

    assert not ({normalize_name(query) for query in searches} & tags)
    engine.dispose()


def test_album_search_supports_artist_context_typos_and_unavailable_tracks() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        artist = _artist(db, "Kai Angel", source_slug="4ngelkai")
        album, _created = upsert_album(
            db,
            artist=artist,
            title="ANGEL MAY CRY 2",
            album_type="album",
            cover_url=None,
            release_date=datetime(2025, 1, 1),
            track_count=3,
            source_name="soundcloud",
            source_external_id="angel-may-cry-2",
            source_url="https://soundcloud.com/4ngelkai/sets/angel-may-cry-2",
            popularity_score=90,
        )
        intro = _track(
            db,
            title="john galliano",
            artists=[artist],
            external_id="john-galliano",
        )
        target = _track(
            db,
            title="millions",
            artists=[artist],
            external_id="millions",
        )
        unavailable = _track(
            db,
            title="hidden cut",
            artists=[artist],
            external_id="hidden-cut",
            is_playable=False,
        )
        for position, track in enumerate((intro, target, unavailable), start=1):
            link_album_track(
                db,
                album_id=album.id,
                track_id=track.id,
                track_number=position,
            )
        db.commit()

        artist_track_matches = search_album_matches(db, "Kai Angel millions")
        assert len(artist_track_matches) == 1
        assert artist_track_matches[0][0].id == album.id
        assert artist_track_matches[0][1] == target.id

        artist_album_matches = search_album_matches(db, "Kai Angel ANGEL MAY CRY 2")
        assert [matched_album.id for matched_album, _track_id in artist_album_matches] == [
            album.id
        ]

        typo_matches = search_album_matches(db, "milions")
        assert len(typo_matches) == 1
        assert typo_matches[0][0].id == album.id
        assert typo_matches[0][1] == target.id

        assert search_album_matches(db, "hidden cut") == []
        payload = album_to_read(artist_track_matches[0][0], artist_track_matches[0][1])

    assert [track.id for track in payload.tracks] == [target.id, intro.id]
    engine.dispose()


def test_album_search_compacts_artist_punctuation_and_hides_empty_releases() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        artist = _artist(db, "M.I.A.", source_slug="mia")
        album, _created = upsert_album(
            db,
            artist=artist,
            title="Kala",
            album_type="album",
            cover_url=None,
            release_date=datetime(2007, 8, 8),
            track_count=1,
            source_name="soundcloud",
            source_external_id="kala",
            source_url="https://soundcloud.com/mia/sets/kala",
            popularity_score=90,
        )
        paper_planes = _track(
            db,
            title="Paper Planes",
            artists=[artist],
            external_id="paper-planes",
        )
        link_album_track(db, album_id=album.id, track_id=paper_planes.id, track_number=1)

        empty_album, _created = upsert_album(
            db,
            artist=artist,
            title="Unavailable Archive",
            album_type="album",
            cover_url=None,
            release_date=datetime(2006, 1, 1),
            track_count=1,
            source_name="soundcloud",
            source_external_id="unavailable-archive",
            source_url="https://soundcloud.com/mia/sets/unavailable-archive",
            popularity_score=1,
        )
        unavailable = _track(
            db,
            title="Hidden",
            artists=[artist],
            external_id="hidden",
            is_playable=False,
        )
        link_album_track(db, album_id=empty_album.id, track_id=unavailable.id, track_number=1)
        db.commit()

        compact_matches = search_album_matches(db, "MIA Paper Planes")
        empty_matches = search_album_matches(db, "Unavailable Archive")

    assert [(item.id, matched_id) for item, matched_id in compact_matches] == [
        (album.id, paper_planes.id)
    ]
    assert empty_matches == []
    engine.dispose()


def test_search_overview_reports_pending_refresh(monkeypatch) -> None:
    monkeypatch.setattr(search_router, "search_local_catalog", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(search_router, "search_album_matches", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(search_router, "get_artists_by_ids", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(search_router, "search_hydration_pending", lambda _query: True)
    monkeypatch.setattr(search_router, "album_hydration_pending", lambda _artist_ids: False)

    result = search_router.search_overview(
        background_tasks=BackgroundTasks(),
        q="Example song",
        db=object(),
    )

    assert isinstance(result, SearchOverview)
    assert result.refresh_pending is True
