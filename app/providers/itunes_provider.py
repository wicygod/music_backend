import json
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import PROVIDER_TIMEOUT_SECONDS
from app.providers.base import ProviderArtistResult, ProviderTrackResult
from app.services.normalization_service import normalize_artist_name


class ItunesProvider:
    name = "itunes"
    base_url = "https://itunes.apple.com/search"

    def search_artist(self, name: str) -> list[ProviderArtistResult]:
        payload = self._get({"term": name, "entity": "musicArtist", "limit": 10})
        results = []
        for item in payload.get("results", []):
            artist_name = item.get("artistName") or item.get("amgArtistName")
            if not artist_name:
                continue
            raw = {**item, "_provider": self.name}
            results.append(
                ProviderArtistResult(
                    external_id=str(item.get("artistId") or item.get("amgArtistId") or artist_name),
                    name=artist_name,
                    avatar_url=None,
                    genres=[item["primaryGenreName"]] if item.get("primaryGenreName") else [],
                    source_url=item.get("artistLinkUrl"),
                    confidence_score=_confidence(name, artist_name),
                    raw=raw,
                )
            )
        return results

    def search_tracks_by_artist(self, name: str, limit: int = 25) -> list[ProviderTrackResult]:
        capped_limit = max(1, min(limit, 100))
        return self.search_tracks(name, capped_limit)

    def search_tracks(self, query: str, limit: int = 25) -> list[ProviderTrackResult]:
        capped_limit = max(1, min(limit, 100))
        payload = self._get({"term": query, "entity": "song", "limit": capped_limit})
        tracks = []
        for item in payload.get("results", []):
            track_name = item.get("trackName")
            artist_name = item.get("artistName")
            if not track_name or not artist_name:
                continue
            duration_ms = item.get("trackTimeMillis")
            raw = {**item, "_provider": self.name}
            tracks.append(
                ProviderTrackResult(
                    external_id=str(item.get("trackId") or item.get("trackViewUrl") or f"{artist_name}:{track_name}"),
                    title=track_name,
                    artist_name=artist_name,
                    album_name=item.get("collectionName"),
                    duration_seconds=round(duration_ms / 1000) if isinstance(duration_ms, int) else None,
                    cover_url=_upgrade_artwork_url(item.get("artworkUrl100")),
                    genre=item.get("primaryGenreName"),
                    source_url=item.get("trackViewUrl"),
                    release_date=item.get("releaseDate"),
                    popularity_score=50.0,
                    raw=raw,
                )
            )
        return tracks

    def _get(self, params: dict[str, object]) -> dict:
        url = f"{self.base_url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "MillionDollarsMusicBackend/0.1"})
        with urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))


def _upgrade_artwork_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("100x100bb", "600x600bb").replace("100x100-75", "600x600-75")


def _confidence(seed_name: str, result_name: str) -> float:
    seed = normalize_artist_name(seed_name)
    result = normalize_artist_name(result_name)
    if not seed or not result:
        return 0.0
    if seed == result:
        return 1.0
    if seed in result or result in seed:
        return 0.85
    return round(SequenceMatcher(None, seed, result).ratio(), 3)
