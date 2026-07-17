import json
import os
from datetime import datetime

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artist import Artist
from app.models.track import Track, TrackArtist
from app.repositories.albums import link_album_track, search_album_matches, upsert_album
from app.services.album_import_service import parse_soundcloud_album_hydration
from app.services.normalization_service import normalize_name, normalize_title
from app.services.serialization_service import album_to_read


def test_soundcloud_album_hydration_requires_the_canonical_artist_profile() -> None:
    playlist = {
        "id": 42,
        "kind": "playlist",
        "title": "Real album",
        "permalink_url": "https://soundcloud.com/real-artist/sets/real-album",
        "user": {"username": "Real Artist", "permalink_url": "https://soundcloud.com/real-artist"},
        "tracks": [{"id": 7, "title": "Target song"}],
    }
    html = f"<script>window.__sc_hydration = {json.dumps([{'hydratable': 'playlist', 'data': playlist}])};</script>"

    assert parse_soundcloud_album_hydration(
        html,
        expected_profile_url="https://soundcloud.com/real-artist",
    ) == playlist
    assert parse_soundcloud_album_hydration(
        html,
        expected_profile_url="https://soundcloud.com/reupload-account",
    ) is None


def test_searching_album_track_returns_the_full_album_with_match_first() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        artist = Artist(
            name="Tuborosho",
            normalized_name=normalize_name("Tuborosho"),
            genres_json="[]",
            source_name="soundcloud",
            source_url="https://soundcloud.com/tuborosho",
            is_canonical=True,
        )
        db.add(artist)
        db.flush()
        album, _created = upsert_album(
            db,
            artist=artist,
            title="Millionaire era",
            album_type="album",
            cover_url="https://i1.sndcdn.com/artworks-test-t500x500.jpg",
            release_date=datetime(2025, 1, 1),
            track_count=3,
            source_name="soundcloud",
            source_external_id="album-42",
            source_url="https://soundcloud.com/tuborosho/sets/millionaire-era",
            popularity_score=80,
        )
        tracks = []
        for index, title in enumerate(("Intro", "Миллионер из трущоб", "Outro"), start=1):
            track = Track(
                title=title,
                normalized_title=normalize_title(title),
                duration_seconds=90,
                tags_json="[]",
                region="ru",
                popularity_score=10,
                quality_score=100,
                is_playable=True,
                source_name="soundcloud",
                source_external_id=f"track-{index}",
                source_url=f"https://soundcloud.com/tuborosho/track-{index}",
                needs_review=False,
            )
            db.add(track)
            db.flush()
            db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
            link_album_track(db, album_id=album.id, track_id=track.id, track_number=index)
            tracks.append(track)
        db.commit()

        matches = search_album_matches(db, "Миллионер из трущоб")
        assert len(matches) == 1
        loaded_album, matched_track_id = matches[0]
        payload = album_to_read(loaded_album, matched_track_id)

    assert payload.track_count == 3
    assert [track.title for track in payload.tracks] == ["Миллионер из трущоб", "Intro", "Outro"]
    assert payload.matched_track_id == tracks[1].id
    engine.dispose()
