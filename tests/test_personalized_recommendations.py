import json
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("MUSIC_APP_AUTH_TOKEN", "test-app-token")
os.environ.setdefault("MUSIC_ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MUSIC_JWT_SECRET", "test-jwt-secret")

from app.database import Base
from app.models.artist import Artist
from app.models.history import ListeningHistory
from app.models.personalization import UserArtistPreference
from app.models.track import Track, TrackArtist
from app.models.user import User
from app.services import recommendation_service
from app.services.normalization_service import normalize_artist_name, normalize_title
from app.services.personalized_ranking_service import (
    ScoredRecommendation,
    choose_weighted_mix,
    rerank_for_diversity,
)
from app.services.recommendation_service import (
    get_personalized_recommendations,
    invalidate_recommendations,
)


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _artist_track(
    db: Session,
    name: str,
    genre: str,
    *,
    playable: bool = True,
    needs_review: bool = False,
    popularity: float = 60.0,
    index: int = 1,
    tags: tuple[str, ...] = (),
) -> tuple[Artist, Track]:
    artist = Artist(
        name=name,
        normalized_name=normalize_artist_name(name),
        region="global",
        needs_review=False,
    )
    db.add(artist)
    db.flush()
    slug = normalize_artist_name(name).replace(" ", "-")
    track = Track(
        title=f"{name} Song {index}",
        normalized_title=normalize_title(f"{name} Song {index}"),
        duration_seconds=180,
        genre=genre,
        popularity_score=popularity,
        quality_score=100.0,
        is_playable=playable,
        source_name="soundcloud",
        source_url=f"https://soundcloud.com/{slug}/song-{index}",
        tags_json=json.dumps(tags),
        needs_review=needs_review,
    )
    db.add(track)
    db.flush()
    db.add(TrackArtist(track_id=track.id, artist_id=artist.id, role="main"))
    return artist, track


def test_cold_start_returns_only_available_catalog_tracks() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="cold-start", nickname="Listener", password_hash="hash")
        db.add(user)
        available = [
            _artist_track(db, f"Cold Artist {index}", genre, popularity=50 + index)[1]
            for index, genre in enumerate(("Rock", "Pop", "Trap", "Ambient"), 1)
        ]
        unavailable = _artist_track(db, "Unavailable Recommendation", "Rock", playable=False)[1]
        review = _artist_track(db, "Review Recommendation", "Pop", needs_review=True)[1]
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=10)
        result_ids = {item.track.id for item in result.items}

        assert result.personalization_active is False
        assert result_ids == {track.id for track in available}
        assert unavailable.id not in result_ids
        assert review.id not in result_ids
        assert {item.recommendation_type for item in result.items} <= {"popular", "exploration"}

    engine.dispose()
    invalidate_recommendations()


def test_genre_and_tag_overlap_produce_similar_artist_recommendation() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="similar-user", nickname="Listener", password_hash="hash")
        db.add(user)
        preferred_artist, preferred_track = _artist_track(
            db,
            "Preferred Rock",
            "Rock",
            popularity=70,
            tags=("shoegaze", "dream rock"),
        )
        similar_artist, similar_track = _artist_track(
            db,
            "Related Rock",
            "Rock",
            popularity=68,
            tags=("shoegaze", "dream rock"),
        )
        _, unrelated_track = _artist_track(db, "Unrelated Classical", "Classical", popularity=67)
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=preferred_artist.id,
                source="onboarding",
                explicit_weight=5.0,
                behavior_weight=0.0,
                weight=5.0,
                explicit_selected=True,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=3)
        by_track = {item.track.id: item for item in result.items}

        assert result.personalization_active is True
        assert by_track[preferred_track.id].recommendation_type == "selected"
        assert by_track[preferred_track.id].reason == (
            "От Preferred Rock - выбран вами при регистрации"
        )
        assert by_track[similar_track.id].recommendation_type == "similar"
        assert by_track[similar_track.id].reason == (
            "Похоже на Preferred Rock - по вашим предпочтениям"
        )
        assert unrelated_track.id in by_track
        assert similar_artist.id != preferred_artist.id

    engine.dispose()
    invalidate_recommendations()


def test_broad_genre_or_provider_popularity_alone_does_not_claim_similarity() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="strict-similarity", nickname="Listener", password_hash="hash")
        db.add(user)
        preferred_artist, _ = _artist_track(
            db,
            "Preferred Rap",
            "Hip-Hop/Rap",
            popularity=75,
            tags=("provider", "soundcloud"),
        )
        _, same_broad_genre = _artist_track(
            db,
            "Generic Rap Upload",
            "Hip-hop & Rap",
            popularity=75,
            tags=("provider", "soundcloud"),
        )
        _, popularity_only = _artist_track(
            db,
            "No Metadata Upload",
            "soundcloud",
            popularity=75,
            tags=("provider", "soundcloud"),
        )
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=preferred_artist.id,
                source="onboarding",
                explicit_weight=5.0,
                behavior_weight=0.0,
                weight=5.0,
                explicit_selected=True,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=10)
        by_track = {item.track.id: item for item in result.items}

        assert by_track[same_broad_genre.id].recommendation_type == "genre"
        assert by_track[popularity_only.id].recommendation_type != "similar"
        assert all(
            item.reason != "Похоже на Preferred Rap - по вашим предпочтениям"
            for item in result.items
        )

    engine.dispose()
    invalidate_recommendations()


def test_provider_credit_to_selected_artist_is_not_labeled_similar_to_itself() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="credit-user", nickname="Listener", password_hash="hash")
        db.add(user)
        preferred_artist, _ = _artist_track(
            db,
            "Preferred Artist",
            "Hip-Hop/Rap",
            popularity=60,
        )
        _, credited_track = _artist_track(
            db,
            "Upload Account",
            "Hip-hop & Rap",
            popularity=75,
            tags=("Preferred Artist",),
        )
        credited_track.title = "Preferred Artist - New Track"
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=preferred_artist.id,
                source="onboarding",
                explicit_weight=5.0,
                behavior_weight=0.0,
                weight=5.0,
                explicit_selected=True,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=10)
        recommendation = next(item for item in result.items if item.track.id == credited_track.id)

        assert recommendation.recommendation_type == "selected"
        assert recommendation.reason == (
            "От Preferred Artist - выбран вами при регистрации"
        )

    engine.dispose()
    invalidate_recommendations()


def test_behavioral_preference_reason_names_the_track_artist() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="behavior-reason", nickname="Listener", password_hash="hash")
        db.add(user)
        artist, track = _artist_track(db, "History Artist", "Ambient", popularity=72)
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=artist.id,
                source="listening",
                explicit_weight=0.0,
                behavior_weight=4.0,
                weight=4.0,
                explicit_selected=False,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=5)
        recommended = next(item for item in result.items if item.track.id == track.id)

        assert recommended.recommendation_type == "selected"
        assert recommended.reason == "От History Artist - на основе ваших предпочтений"

    engine.dispose()
    invalidate_recommendations()


def test_diversity_reranker_avoids_adjacent_artist_and_caps_two_in_ten() -> None:
    candidates = [
        ScoredRecommendation(
            item=f"dominant-{index}",
            stable_key=f"dominant-{index}",
            artist_id=1,
            genre_key="rock",
            recommendation_type="selected",
            reason="preferred",
            score=100 - index,
        )
        for index in range(8)
    ]
    candidates.extend(
        ScoredRecommendation(
            item=f"other-{artist_id}-{index}",
            stable_key=f"other-{artist_id}-{index}",
            artist_id=artist_id,
            genre_key=f"genre-{artist_id % 3}",
            recommendation_type="similar",
            reason="similar",
            score=80 - artist_id - index,
        )
        for artist_id in range(2, 10)
        for index in range(2)
    )

    ranked = rerank_for_diversity(candidates, rotation_key="test-user")
    head = ranked[:10]
    head_artists = [item.artist_id for item in head]

    assert all(left != right for left, right in zip(head_artists, head_artists[1:]))
    assert max(Counter(head_artists).values()) <= 2


def test_weighted_bucket_selection_round_robins_artists_before_repeats() -> None:
    candidates = [
        ScoredRecommendation(
            item=f"dominant-{index}",
            stable_key=f"dominant-{index}",
            artist_id=1,
            genre_key="rock",
            recommendation_type="selected",
            reason="selected",
            score=100 - index,
        )
        for index in range(8)
    ]
    candidates.extend(
        ScoredRecommendation(
            item=f"other-{artist_id}",
            stable_key=f"other-{artist_id}",
            artist_id=artist_id,
            genre_key="rock",
            recommendation_type="selected",
            reason="selected",
            score=50 - artist_id,
        )
        for artist_id in range(2, 6)
    )

    ranked = choose_weighted_mix(
        candidates,
        limit=6,
        shares={"selected": 1.0},
        rotation_key="round-robin",
    )

    counts = Counter(item.artist_id for item in ranked)
    assert len(counts) == 5
    assert counts[1] == 2


def test_hidden_artist_is_excluded_from_recommendations() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="hidden-user", nickname="Listener", password_hash="hash")
        db.add(user)
        hidden_artist, hidden_track = _artist_track(
            db,
            "Hidden Artist",
            "Rock",
            popularity=100,
        )
        _, visible_track = _artist_track(
            db,
            "Visible Artist",
            "Pop",
            popularity=60,
        )
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=hidden_artist.id,
                source="hide",
                explicit_weight=0.0,
                behavior_weight=-20.0,
                weight=-20.0,
                explicit_selected=False,
                is_hidden=True,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=10)
        result_ids = {item.track.id for item in result.items}

        assert visible_track.id in result_ids
        assert hidden_track.id not in result_ids

    engine.dispose()
    invalidate_recommendations()


def test_selected_artist_outside_global_candidate_limit_is_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidate_recommendations()
    engine = _engine()
    constrained_config = replace(
        recommendation_service.RECOMMENDATION_CONFIG,
        candidate_limit=50,
    )
    monkeypatch.setattr(
        recommendation_service,
        "RECOMMENDATION_CONFIG",
        constrained_config,
    )

    with Session(engine) as db:
        user = User(login="candidate-pool-user", nickname="Listener", password_hash="hash")
        db.add(user)
        selected_artist, selected_track = _artist_track(
            db,
            "Low Popularity Choice",
            "Ambient",
            popularity=0,
        )
        for index in range(51):
            _artist_track(
                db,
                f"Global Pool Artist {index}",
                "Pop",
                popularity=100,
                index=index + 1,
            )
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=selected_artist.id,
                source="onboarding",
                explicit_weight=5.0,
                behavior_weight=0.0,
                weight=5.0,
                explicit_selected=True,
                is_hidden=False,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=10)

        assert selected_track.id in {item.track.id for item in result.items}

    engine.dispose()
    invalidate_recommendations()


def test_genre_alias_pool_discovers_relevant_artist_outside_global_top() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="alias-pool-user", nickname="Listener", password_hash="hash")
        db.add(user)
        preferred_artist, _ = _artist_track(
            db,
            "Preferred Alias Artist",
            "Hip-Hop/Rap",
            popularity=1,
            tags=("dark trap",),
        )
        _, related_track = _artist_track(
            db,
            "Related Alias Artist",
            "Hip-hop & Rap",
            popularity=1,
            tags=("dark trap",),
        )
        for index in range(55):
            _artist_track(
                db,
                f"Popular Unrelated {index}",
                "Classical",
                popularity=100,
                index=index + 1,
            )
        db.flush()
        db.add(
            UserArtistPreference(
                user_id=user.id,
                artist_id=preferred_artist.id,
                source="onboarding",
                explicit_weight=5.0,
                behavior_weight=0.0,
                weight=5.0,
                explicit_selected=True,
            )
        )
        db.commit()

        result = get_personalized_recommendations(db, user_id=user.id, limit=100)
        by_track = {item.track.id: item for item in result.items}

        assert related_track.id in by_track
        assert by_track[related_track.id].recommendation_type == "similar"

    engine.dispose()
    invalidate_recommendations()


def test_negative_artist_weight_lowers_all_tracks_from_that_artist() -> None:
    invalidate_recommendations()
    engine = _engine()
    with Session(engine) as db:
        user = User(login="negative-weight-user", nickname="Listener", password_hash="hash")
        db.add(user)
        negative_artist, first_track = _artist_track(
            db,
            "Skipped Artist",
            "Rock",
            popularity=70,
            index=1,
        )
        second_track = Track(
            title="Skipped Artist Song 2",
            normalized_title=normalize_title("Skipped Artist Song 2"),
            duration_seconds=180,
            genre="Rock",
            popularity_score=70,
            quality_score=100.0,
            is_playable=True,
            source_name="soundcloud",
            source_url="https://soundcloud.com/skipped-artist/song-2",
            needs_review=False,
        )
        db.add(second_track)
        db.flush()
        db.add(
            TrackArtist(
                track_id=second_track.id,
                artist_id=negative_artist.id,
                role="main",
            )
        )
        _, neutral_track = _artist_track(
            db,
            "Neutral Artist",
            "Rock",
            popularity=70,
        )
        db.commit()
        candidates = [first_track, second_track, neutral_track]

        baseline = recommendation_service._score_candidates(
            db,
            candidates=candidates,
            user_id=user.id,
            artist_preferences={},
            explicit_artist_ids=set(),
            hidden_artist_ids=set(),
            personalization_active=False,
        )
        penalized = recommendation_service._score_candidates(
            db,
            candidates=candidates,
            user_id=user.id,
            artist_preferences={negative_artist.id: -10.0},
            explicit_artist_ids=set(),
            hidden_artist_ids=set(),
            personalization_active=False,
        )
        baseline_scores = {item.item.id: item.score for item in baseline}
        penalized_scores = {item.item.id: item.score for item in penalized}

        assert penalized_scores[first_track.id] < baseline_scores[first_track.id]
        assert penalized_scores[second_track.id] < baseline_scores[second_track.id]
        assert penalized_scores[neutral_track.id] == pytest.approx(
            baseline_scores[neutral_track.id]
        )

    engine.dispose()
    invalidate_recommendations()


def test_completed_detailed_event_counts_as_listened_and_recent() -> None:
    engine = _engine()
    with Session(engine) as db:
        user = User(login="detailed-history-user", nickname="Listener", password_hash="hash")
        db.add(user)
        artist, track = _artist_track(db, "Detailed Artist", "Rock")
        db.flush()
        now = datetime.utcnow()
        db.add(
            ListeningHistory(
                user_id=f"account:{user.id}",
                track_id=track.id,
                event_id="completed-detailed-event-0001",
                artist_id=artist.id,
                play_count=1,
                played_at=now,
                started_at=now,
                listened_duration_seconds=180,
                track_duration_seconds=180,
                completion_ratio=1.0,
                completed=True,
                skipped=False,
                context="home",
                created_at=now,
            )
        )
        db.commit()

        listened, recent_ids, quick_skips = recommendation_service._history_signals(
            db,
            user_id=user.id,
        )

        assert listened[track.id] == 1
        assert track.id in recent_ids
        assert quick_skips.get(track.id, 0) == 0

    engine.dispose()
