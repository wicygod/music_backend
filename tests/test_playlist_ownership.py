from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.track import Track
from app.repositories.playlists import (
    add_track_to_playlist,
    create_playlist,
    get_playlist,
    remove_track_from_playlist,
)


def test_playlist_mutations_are_scoped_to_the_owner() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        track = Track(title="Owned song", normalized_title="owned song", is_playable=True)
        db.add(track)
        db.commit()
        db.refresh(track)

        playlist = create_playlist(db, name="Private", user_id="account:1")

        assert get_playlist(db, playlist.id, user_id="account:2") is None
        assert add_track_to_playlist(
            db,
            playlist_id=playlist.id,
            track_id=track.id,
            user_id="account:2",
        ) is None

        owned = add_track_to_playlist(
            db,
            playlist_id=playlist.id,
            track_id=track.id,
            user_id="account:1",
        )
        assert owned is not None
        assert [item.track.id for item in owned.track_links] == [track.id]

        assert remove_track_from_playlist(
            db,
            playlist_id=playlist.id,
            track_id=track.id,
            user_id="account:2",
        ) is None
        still_owned = get_playlist(db, playlist.id, user_id="account:1")
        assert still_owned is not None
        assert [item.track.id for item in still_owned.track_links] == [track.id]

    engine.dispose()
