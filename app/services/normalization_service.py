import re


_PUNCTUATION_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_ARTIST_ALLOWED_PUNCTUATION_RE = re.compile(r"[^\w\s#.&'’`/-]+", re.UNICODE)
_QUOTE_RE = re.compile(r"[’`´]")
_SPACES_RE = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    return normalize_artist_name(value)


def normalize_title(value: str) -> str:
    return _normalize(value)


def normalize_track_title_for_dedupe(value: str) -> str:
    cleaned = value.lower()
    cleaned = re.sub(r"\([^)]*(album version|single|remaster|radio edit|explicit|clean)[^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\[[^]]*(album version|single|remaster|radio edit|explicit|clean)[^]]*\]", " ", cleaned)
    cleaned = re.sub(r"\s+-\s+(single|album version|radio edit|remaster).*?$", " ", cleaned)
    return _normalize(cleaned)


def normalize_artist_name(name: str) -> str:
    cleaned = _QUOTE_RE.sub("'", name.strip()).replace("Ё", "Е").replace("ё", "е")
    cleaned = _ARTIST_ALLOWED_PUNCTUATION_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s*([/.&-])\s*", r" \1 ", cleaned)
    cleaned = _SPACES_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def clean_display_artist_name(name: str) -> str:
    cleaned = _QUOTE_RE.sub("'", name.strip())
    return _SPACES_RE.sub(" ", cleaned).strip()


def detect_artist_region(name: str) -> str:
    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", name))
    has_latin = bool(re.search(r"[A-Za-z]", name))
    if has_cyrillic:
        return "ru"
    if has_latin:
        return "global"
    return "unknown"


def _normalize(value: str) -> str:
    lowered = value.strip().lower().replace("ё", "е")
    without_punctuation = _PUNCTUATION_RE.sub(" ", lowered)
    return _SPACES_RE.sub(" ", without_punctuation).strip()
