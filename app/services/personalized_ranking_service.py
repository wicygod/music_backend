from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import blake2b
from typing import Generic, Iterable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ScoredRecommendation(Generic[T]):
    item: T
    stable_key: str
    artist_id: int | None
    genre_key: str
    recommendation_type: str
    reason: str
    score: float


def choose_weighted_mix(
    candidates: Iterable[ScoredRecommendation[T]],
    *,
    limit: int,
    shares: dict[str, float],
    rotation_key: str,
) -> list[ScoredRecommendation[T]]:
    """Take a quota-based mix, then fill gaps from the best remaining items.

    Buckets are intentionally soft: a small catalog must still return a full
    feed, while a sufficiently rich catalog follows the configured shares.
    """

    requested = max(0, int(limit))
    if requested == 0:
        return []

    unique: dict[str, ScoredRecommendation[T]] = {}
    for candidate in candidates:
        if candidate.stable_key and candidate.stable_key not in unique:
            unique[candidate.stable_key] = candidate

    ordered = sorted(
        unique.values(),
        key=lambda candidate: (
            candidate.score,
            _stable_fraction(rotation_key, candidate.stable_key),
        ),
        reverse=True,
    )
    selected: list[ScoredRecommendation[T]] = []
    selected_keys: set[str] = set()
    remaining_slots = min(requested, len(ordered))

    normalized_shares = {key: max(0.0, float(value)) for key, value in shares.items()}
    total_share = sum(normalized_shares.values()) or 1.0
    quotas = {
        key: int(requested * value / total_share)
        for key, value in normalized_shares.items()
    }
    unassigned = requested - sum(quotas.values())
    for key in sorted(normalized_shares, key=normalized_shares.get, reverse=True):
        if unassigned <= 0:
            break
        quotas[key] += 1
        unassigned -= 1

    for bucket, quota in quotas.items():
        if quota <= 0:
            continue
        bucket_items = [item for item in ordered if item.recommendation_type == bucket]
        for item in bucket_items[:quota]:
            selected.append(item)
            selected_keys.add(item.stable_key)
            remaining_slots -= 1

    if remaining_slots > 0:
        for item in ordered:
            if item.stable_key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(item.stable_key)
            remaining_slots -= 1
            if remaining_slots <= 0:
                break

    return rerank_for_diversity(selected, rotation_key=rotation_key)


def rerank_for_diversity(
    candidates: Iterable[ScoredRecommendation[T]],
    *,
    rotation_key: str,
    window_size: int = 10,
    max_artist_in_window: int = 2,
) -> list[ScoredRecommendation[T]]:
    """Greedy rerank with no adjacent artist and a soft two-in-ten cap."""

    remaining = list(candidates)
    selected: list[ScoredRecommendation[T]] = []
    window_size = max(2, int(window_size))
    max_artist_in_window = max(1, int(max_artist_in_window))

    while remaining:
        recent = selected[-(window_size - 1):]
        recent_artists = Counter(item.artist_id for item in recent if item.artist_id is not None)
        previous_artist = selected[-1].artist_id if selected else None
        previous_genre = selected[-1].genre_key if selected else ""

        eligible = [
            item
            for item in remaining
            if (
                item.artist_id is None
                or (
                    item.artist_id != previous_artist
                    and recent_artists[item.artist_id] < max_artist_in_window
                )
            )
        ]
        if not eligible:
            eligible = [item for item in remaining if item.artist_id != previous_artist]
        if not eligible:
            eligible = list(remaining)

        chosen = max(
            eligible,
            key=lambda item: (
                item.score
                + (0.12 if item.genre_key and item.genre_key != previous_genre else 0.0)
                + _stable_fraction(rotation_key, item.stable_key) * 0.01
            ),
        )
        selected.append(chosen)
        remaining.remove(chosen)

    return selected


def _stable_fraction(rotation_key: str, stable_key: str) -> float:
    digest = blake2b(f"{rotation_key}:{stable_key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float((1 << 64) - 1)
