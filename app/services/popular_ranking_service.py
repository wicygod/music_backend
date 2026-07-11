from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
from math import ceil
from typing import Generic, Iterable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PopularCandidate(Generic[T]):
    """Provider-agnostic input for the home-feed diversity ranker."""

    item: T
    stable_key: str
    artist_key: str
    genre_key: str = "unknown"
    region_key: str = "unknown"
    popularity_score: float = 0.0
    quality_score: float = 0.0


def rank_popular_candidates(
    candidates: Iterable[PopularCandidate[T]],
    *,
    limit: int,
    rotation_key: str,
    head_size: int = 24,
    head_artist_cap: int = 2,
    artist_gap: int = 3,
) -> list[PopularCandidate[T]]:
    """Return a relevant but deliberately varied, deterministic popular feed.

    The order remains stable for the supplied rotation key. Callers normally use
    a key made from the UTC date and user id, so the shelf rotates once a day
    without jumping around on every refresh.
    """

    requested_limit = max(0, int(limit))
    if requested_limit == 0:
        return []

    unique: list[PopularCandidate[T]] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        stable_key = str(candidate.stable_key or "").strip()
        if not stable_key or stable_key in seen_keys:
            continue
        seen_keys.add(stable_key)
        unique.append(candidate)

    if not unique:
        return []

    requested_limit = min(requested_limit, len(unique))
    head_size = min(max(0, int(head_size)), requested_limit)
    artist_gap = max(0, int(artist_gap))
    head_artist_cap = max(1, int(head_artist_cap))
    overall_artist_cap = min(8, max(2, ceil(requested_limit * 0.05)))
    available_artists = {_clean_key(item.artist_key) for item in unique}
    target_head_artists = min(len(available_artists), ceil(head_size / head_artist_cap))
    useful_genres = {
        _clean_key(item.genre_key)
        for item in unique
        if _clean_key(item.genre_key) != "unknown"
    }
    genre_cap = max(1, ceil(min(30, requested_limit) * 0.4))

    remaining = list(unique)
    selected: list[PopularCandidate[T]] = []
    artist_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}
    region_counts: dict[str, int] = {}

    while remaining and len(selected) < requested_limit:
        position = len(selected)
        in_head = position < head_size
        current_cap = head_artist_cap if in_head else overall_artist_cap
        recent_artists = {
            _clean_key(item.artist_key)
            for item in selected[-artist_gap:]
        }

        require_new_artist = False
        if in_head and target_head_artists:
            unique_selected = len(artist_counts)
            slots_after_pick = head_size - position - 1
            require_new_artist = unique_selected < target_head_artists and (
                slots_after_pick < target_head_artists - unique_selected
            )

        eligible = _eligible_candidates(
            remaining,
            artist_counts=artist_counts,
            genre_counts=genre_counts,
            artist_cap=current_cap,
            recent_artists=recent_artists,
            require_new_artist=require_new_artist,
            enforce_genre=len(useful_genres) > 1 and position < 30,
            genre_cap=genre_cap,
        )
        if not eligible:
            eligible = _eligible_candidates(
                remaining,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                artist_cap=current_cap,
                recent_artists=recent_artists,
                require_new_artist=require_new_artist,
                enforce_genre=False,
                genre_cap=genre_cap,
            )
        if not eligible:
            eligible = _eligible_candidates(
                remaining,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                artist_cap=current_cap,
                recent_artists=set(),
                require_new_artist=False,
                enforce_genre=False,
                genre_cap=genre_cap,
            )
        if not eligible:
            # A very small or single-artist catalog must still be usable. Caps
            # are relaxed only after every diverse option has been exhausted.
            eligible = list(remaining)

        chosen = max(
            eligible,
            key=lambda item: _selection_score(
                item,
                rotation_key=rotation_key,
                artist_counts=artist_counts,
                genre_counts=genre_counts,
                region_counts=region_counts,
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)

        artist_key = _clean_key(chosen.artist_key)
        genre_key = _clean_key(chosen.genre_key)
        region_key = _clean_key(chosen.region_key)
        artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        if genre_key != "unknown":
            genre_counts[genre_key] = genre_counts.get(genre_key, 0) + 1
        if region_key != "unknown":
            region_counts[region_key] = region_counts.get(region_key, 0) + 1

    return selected


def _eligible_candidates(
    candidates: Iterable[PopularCandidate[T]],
    *,
    artist_counts: dict[str, int],
    genre_counts: dict[str, int],
    artist_cap: int,
    recent_artists: set[str],
    require_new_artist: bool,
    enforce_genre: bool,
    genre_cap: int,
) -> list[PopularCandidate[T]]:
    result: list[PopularCandidate[T]] = []
    for candidate in candidates:
        artist_key = _clean_key(candidate.artist_key)
        genre_key = _clean_key(candidate.genre_key)
        if artist_counts.get(artist_key, 0) >= artist_cap:
            continue
        if artist_key in recent_artists:
            continue
        if require_new_artist and artist_key in artist_counts:
            continue
        if enforce_genre and genre_key != "unknown" and genre_counts.get(genre_key, 0) >= genre_cap:
            continue
        result.append(candidate)
    return result


def _selection_score(
    candidate: PopularCandidate[T],
    *,
    rotation_key: str,
    artist_counts: dict[str, int],
    genre_counts: dict[str, int],
    region_counts: dict[str, int],
) -> float:
    artist_key = _clean_key(candidate.artist_key)
    genre_key = _clean_key(candidate.genre_key)
    region_key = _clean_key(candidate.region_key)
    relevance = float(candidate.popularity_score) * 0.72 + float(candidate.quality_score) * 0.28
    rotation = _stable_fraction(rotation_key, candidate.stable_key) * 4.0
    artist_penalty = artist_counts.get(artist_key, 0) * 16.0
    genre_penalty = genre_counts.get(genre_key, 0) * 1.2 if genre_key != "unknown" else 0.0
    region_penalty = region_counts.get(region_key, 0) * 0.15 if region_key != "unknown" else 0.0
    return relevance + rotation - artist_penalty - genre_penalty - region_penalty


def _stable_fraction(rotation_key: str, stable_key: str) -> float:
    digest = blake2b(f"{rotation_key}:{stable_key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float((1 << 64) - 1)


def _clean_key(value: str | None) -> str:
    return str(value or "").strip().lower() or "unknown"
