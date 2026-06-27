from app.providers.base import MetadataProvider, ProviderTrackResult
from app.providers.itunes_provider import ItunesProvider


class ProviderManager:
    def __init__(self, providers: list[MetadataProvider] | None = None) -> None:
        self.providers = providers or [ItunesProvider()]

    def search_tracks_by_artist(self, name: str, limit: int = 25) -> list[ProviderTrackResult]:
        merged: list[ProviderTrackResult] = []
        seen: set[tuple[str, str]] = set()
        for provider in self.providers:
            for track in provider.search_tracks_by_artist(name, limit=limit):
                key = (track.raw.get("_provider", provider.name), track.external_id)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(track)
        return merged[:limit]

    def search_tracks(self, query: str, limit: int = 25) -> list[ProviderTrackResult]:
        merged: list[ProviderTrackResult] = []
        seen: set[tuple[str, str]] = set()
        for provider in self.providers:
            for track in provider.search_tracks(query, limit=limit):
                key = (track.raw.get("_provider", provider.name), track.external_id)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(track)
        return merged[:limit]
