import re
from difflib import SequenceMatcher


_PUNCTUATION_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_ARTIST_ALLOWED_PUNCTUATION_RE = re.compile(r"[^\w\s#.&'’`/-]+", re.UNICODE)
_QUOTE_RE = re.compile(r"[’`´]")
_SPACES_RE = re.compile(r"\s+")


def normalize_name(value: str) -> str:
    return normalize_artist_name(value)


def normalize_title(value: str) -> str:
    return _normalize(value)


def normalize_search_text(value: str) -> str:
    """Normalize free-form search text consistently across catalog entities."""

    return _normalize(value)


def compact_search_text(value: str) -> str:
    """Return a punctuation/spacing agnostic key for names such as M.I.A."""

    return normalize_search_text(value).replace(" ", "")


def search_tokens(value: str, *, minimum_length: int = 2) -> list[str]:
    return [
        token
        for token in normalize_search_text(value).split()
        if len(token) >= max(1, int(minimum_length))
    ]


def search_token_matches(token: str, normalized_text: str) -> bool:
    """Match a token exactly or with one conservative user typo.

    SQL first narrows the candidate pool. This bounded comparison then handles
    a missing character, an extra character, or an adjacent transposition
    without turning the whole catalog into a fuzzy full-table search.
    """

    if token in normalized_text:
        return True
    if len(token) < 4:
        return False
    for candidate in normalized_text.split():
        if len(candidate) < 3 or abs(len(token) - len(candidate)) > 2:
            continue
        max_distance = 1 if max(len(token), len(candidate)) <= 6 else 2
        if _damerau_levenshtein_distance(token, candidate, max_distance) <= max_distance:
            return True
        if SequenceMatcher(None, token, candidate).ratio() >= 0.84:
            return True
    return False


def all_search_tokens_match(tokens: list[str], normalized_text: str) -> bool:
    return bool(tokens) and all(search_token_matches(token, normalized_text) for token in tokens)


def search_candidate_fragments(value: str) -> list[str]:
    """Build a small portable LIKE prefilter for fuzzy candidate retrieval."""

    fragments: list[str] = []
    for token in search_tokens(value):
        compact = compact_search_text(token)
        if len(compact) < 3:
            continue
        width = 2 if len(compact) <= 5 else 3
        token_fragments = [compact[index:index + width] for index in range(len(compact) - width + 1)]
        if len(token_fragments) > 3:
            token_fragments = [
                token_fragments[0],
                token_fragments[len(token_fragments) // 2],
                token_fragments[-1],
            ]
        for fragment in token_fragments:
            if fragment not in fragments:
                fragments.append(fragment)
    return fragments[:9]


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


def _damerau_levenshtein_distance(left: str, right: str, cutoff: int) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > cutoff:
        return cutoff + 1
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_char in enumerate(right, start=1):
            substitution = previous[right_index - 1] + int(left_char != right_char)
            insertion = current[right_index - 1] + 1
            deletion = previous[right_index] + 1
            distance = min(substitution, insertion, deletion)
            if (
                previous_previous is not None
                and left_index > 1
                and right_index > 1
                and left_char == right[right_index - 2]
                and left[left_index - 2] == right_char
            ):
                distance = min(distance, previous_previous[right_index - 2] + 1)
            current.append(distance)
            row_minimum = min(row_minimum, distance)
        if row_minimum > cutoff:
            return cutoff + 1
        previous_previous, previous = previous, current
    return previous[-1]
