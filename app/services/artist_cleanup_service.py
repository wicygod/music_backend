import re
from urllib.parse import urlparse

from app.services.normalization_service import clean_display_artist_name, normalize_name


TITLE_ARTIST_SEP_RE = re.compile(r"\s+(?:-|\u2013|\u2014|:)\s+")
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
POPULAR_BRACKET_RE = re.compile(
    r"[\[(][^\])]*(?:sped|speed|slowed|reverb|8d|nightcore|remix|edit|bootleg|mashup|lyrics?|demo|snippet|ai)[^\])]*[\])]",
    re.IGNORECASE,
)
POPULAR_VARIANT_RE = re.compile(
    r"\b("
    r"sped\s*up|speed\s*up|speedup|spedup|slowed|slow\s*\+\s*reverb|slowed\s*\+\s*reverb|"
    r"reverb|8d|nightcore|hardstyle|remix|rmx|mix|edit|bootleg|mashup|bass\s*boost(?:ed)?|visualizer|lyrics?|"
    r"breakcore|cover|\u043a\u0430\u0432\u0435\u0440|snippet|preview|demo|ai|mylancore|instrumental|karaoke|version|\u0432\u0435\u0440\u0441\u0438\u044f|"
    r"intro|outro|\u0438\u043d\u0442\u0440\u043e|\u0430\u0443\u0442\u0440\u043e"
    r")\b",
    re.IGNORECASE,
)
POPULAR_TRAILING_VARIANT_RE = re.compile(
    r"\b("
    r"sped\s*up|speed\s*up|speedup|spedup|slowed|reverb|8d|nightcore|hardstyle|remix|rmx|mix|edit|"
    r"bootleg|mashup|breakcore|cover|\u043a\u0430\u0432\u0435\u0440|snippet|preview|demo|ai|version|\u0432\u0435\u0440\u0441\u0438\u044f|"
    r"mylancore|instrumental|karaoke|bass\s*boost(?:ed)?|intro|outro|"
    r"\u0438\u043d\u0442\u0440\u043e|\u0430\u0443\u0442\u0440\u043e"
    r")\b.*$",
    re.IGNORECASE,
)
POPULAR_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s]*)?",
    re.IGNORECASE,
)
POPULAR_FILE_EXT_RE = re.compile(r"\.(?:mp3|m4a|webm|wav|flac|aac|ogg)\b", re.IGNORECASE)
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*", re.IGNORECASE)
POPULAR_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
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
    if POPULAR_DOMAIN_RE.search(cleaned):
        return False
    if NOISE_ARTIST_RE.search(normalized):
        return False
    return bool(re.search(r"[A-Za-z\u0410-\u042f\u0430-\u044f\u0401\u04510-9]", cleaned))


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
    return re.sub(r"[^a-z0-9\u0430-\u044f\u0451]+", "", normalize_name(value))


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


def is_low_value_popular_variant(title: str | None) -> bool:
    """Return variants that should need unusually strong chart evidence.

    They are not deleted from search or a user's library.  The public chart is
    simply conservative about slowed/reverb, bass-boosted, demos, AI covers and
    similar reuploads unless provider or listener signals prove real demand.
    """

    return bool(POPULAR_BRACKET_RE.search(title or "") or POPULAR_VARIANT_RE.search(title or ""))


def popular_track_key(title: str | None, artist: str | None = None) -> str:
    display_artist = primary_artist_segment(artist or artist_from_title(title) or "")
    display_title = title_without_artist_prefix(title)
    artist_key = normalize_name(display_artist)
    raw = _popular_title_core(display_title, artist_key)
    if not raw and title and display_title != title:
        raw = _popular_title_core(title, artist_key)
    return raw


def _popular_title_core(value: str | None, artist_key: str = "") -> str:
    raw = (value or "").lower()
    raw = POPULAR_DOMAIN_RE.sub(" ", raw)
    raw = POPULAR_FILE_EXT_RE.sub(" ", raw)
    raw = POPULAR_BRACKET_RE.sub(" ", raw)
    raw = POPULAR_TRAILING_VARIANT_RE.sub(" ", raw)
    raw = POPULAR_VARIANT_RE.sub(" ", raw)
    raw = re.sub(r"\b(?:prod|produced\s+by)\b.*$", " ", raw, flags=re.IGNORECASE)
    raw = POPULAR_NON_WORD_RE.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if artist_key:
        raw = re.sub(rf"\b{re.escape(artist_key)}\b", " ", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def primary_artist_segment(artist: str | None) -> str:
    if not artist:
        return ""
    parts = [part.strip() for part in ARTIST_SPLIT_RE.split(artist) if part.strip()]
    return clean_display_artist_name(parts[0] if parts else artist)
