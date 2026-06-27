from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ProviderArtistResult:
    external_id: str
    name: str
    avatar_url: str | None = None
    genres: list[str] = field(default_factory=list)
    source_url: str | None = None
    confidence_score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderTrackResult:
    external_id: str
    title: str
    artist_name: str
    album_name: str | None = None
    duration_seconds: int | None = None
    cover_url: str | None = None
    genre: str | None = None
    source_url: str | None = None
    release_date: str | None = None
    popularity_score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


class MetadataProvider(Protocol):
    name: str

    def search_artist(self, name: str) -> list[ProviderArtistResult]:
        ...

    def search_tracks_by_artist(self, name: str, limit: int = 25) -> list[ProviderTrackResult]:
        ...

    def search_tracks(self, query: str, limit: int = 25) -> list[ProviderTrackResult]:
        ...
