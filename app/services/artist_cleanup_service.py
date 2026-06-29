import re
from urllib.parse import urlparse

from app.services.normalization_service import clean_display_artist_name, normalize_name


TITLE_ARTIST_SEP_RE = re.compile(r"\s+(?:-|–|—|:)\s+")
NOISE_ARTIST_RE = re.compile(
    r"\b("
    r"official|audio|video|lyrics?|lyric|visualizer|remix|sped|speed|slowed|reverb|8d|nightcore|"
    r"prod|type\s+beat|full\s+album|playlist|mix|compilation|archive|hub|radio"
    r")\b",
    re.IGNORECASE,
)
SUSPICIOUS_UPLOADER_RE = re.compile(
    r"\b("
    r"hub|archive|archives|vault|reupload|reuploads|upload|uploads|fan|fans|daily|unreleased|"
    r"music\d+|track\d+|user\d+|soundcloud|youtube"
    r")\b",
    re.IGNORECASE,
)
TRUSTED_COMMUNITIES = {
    "gothboiclique",
    "goth boi clique",
    "gbc",
    "ripsquad",
    "sadboys",
    "year0001",
}


def _looks_like_artist(value: str) -> bool:
    cleaned = clean_display_artist_name(value)
    normalized = normalize_name(cleaned)
    if not cleaned or len(cleaned) > 80:
        return False
    if len(normalized.split()) > 8:
        return False
    if any(part in normalized for part in ("www", "http", ".com", ".ru", ".net", ".org")):
        return False
    if NOISE_ARTIST_RE.search(normalized):
        return False
    return bool(re.search(r"[A-Za-zА-Яа-яЁё0-9]", cleaned))


def artist_from_title(title: str | None) -> str | None:
    if not title:
        return None
    parts = TITLE_ARTIST_SEP_RE.split(title.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    artist = clean_display_artist_name(parts[0])
    return artist if _looks_like_artist(artist) else None


def title_without_artist_prefix(title: str | None) -> str:
    if not title:
        return ""
    parts = TITLE_ARTIST_SEP_RE.split(title.strip(), maxsplit=1)
    if len(parts) == 2 and _looks_like_artist(parts[0]):
        return clean_display_artist_name(parts[1])
    return clean_display_artist_name(title)


def is_trusted_music_uploader(uploader: str | None, expected_artist: str | None = None) -> bool:
    normalized = normalize_name(uploader or "")
    if not normalized:
        return False
    expected = normalize_name(expected_artist or "")
    if expected and (normalized == expected or expected in normalized or normalized in expected):
        return True
    if normalized in TRUSTED_COMMUNITIES:
        return True
    return any(word in normalized for word in ("official", "records", "recordings", "music"))


def is_suspicious_uploader(uploader: str | None) -> bool:
    normalized = normalize_name(uploader or "")
    if not normalized:
        return True
    if normalized in TRUSTED_COMMUNITIES:
        return False
    return bool(SUSPICIOUS_UPLOADER_RE.search(normalized))


def clean_provider_artist(title: str | None, uploader: str | None, fallback: str | None = None) -> str | None:
    parsed_artist = artist_from_title(title)
    if parsed_artist:
        return parsed_artist
    if is_suspicious_uploader(uploader):
        return None
    return clean_display_artist_name(uploader or fallback or "") or None


def provider_authority_score(title: str | None, uploader: str | None, query: str | None = None) -> int:
    parsed_artist = artist_from_title(title)
    score = 0
    if parsed_artist:
        score += 15
    if is_trusted_music_uploader(uploader, parsed_artist or query):
        score += 25
    if is_suspicious_uploader(uploader) and not parsed_artist:
        score -= 40
    return score


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "", normalize_name(value))


def source_profile_matches_artist(source_url: str | None, artist: str | None) -> bool:
    if not source_url or not artist:
        return False
    path_parts = [part for part in urlparse(str(source_url)).path.split("/") if part]
    if not path_parts:
        return False
    profile = _compact(path_parts[0])
    expected = _compact(artist)
    return bool(profile and expected and (profile == expected or expected in profile or profile in expected))


def has_clean_artist_signal(title: str | None, artist: str | None, source_url: str | None = None) -> bool:
    parsed_artist = artist_from_title(title)
    if parsed_artist:
        return True
    if source_profile_matches_artist(source_url, artist):
        return True
    return is_trusted_music_uploader(artist)
