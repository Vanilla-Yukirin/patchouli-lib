"""Pure literal matching and safe FTS5 candidate terms for search v2.

This module does not read a database or decide which files are indexable text.
An index writer must add each complete, explicitly selected field separately;
an FTS candidate is only a superset and must be checked with ``find_page_hits``.
This module is used by the registered search endpoint but does not register
or enable that endpoint on its own.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from patchouli_lib.content.file_manifest import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAGE,
    MAX_PAGE_BYTES,
)
from patchouli_lib.search.ngram import MAX_UNIQUE_TERMS

GRAM_VERSION = "unicode-codepoint-1-2-3-NFC-casefold-NFC-v1"
UNICODE_DATA_VERSION = unicodedata.unidata_version
MAX_QUERY_BYTES = 32 * 1024
MAX_QUERY_KEYWORDS = 256
MAX_NORMALIZED_QUERY_BYTES = 4 * MAX_QUERY_BYTES
MAX_NORMALIZED_FIELD_BYTES = 4 * MAX_FILE_BYTES
MAX_PAGE_MATCH_WORK = 512 * 1024 * 1024
_GRAM_CHUNK_CODEPOINTS = 4096
_MAX_CACHED_GRAM_CHUNKS = 1024

HitSource = Literal["title", "body", "file_name", "file_text"]


@dataclass(frozen=True, slots=True)
class LiteralHit:
    """One distinct keyword in one complete field (never across fields)."""

    keyword: str
    source: HitSource
    file_name: str | None = None


def _input_byte_size(value: str | bytes) -> int:
    if type(value) is bytes:
        return len(value)
    if type(value) is str:
        try:
            return len(value.encode("utf-8", errors="strict"))
        except UnicodeError:
            raise ValueError("Literal must be valid UTF-8.") from None
    raise TypeError("Literal must be str or exact bytes.")


def normalize_literal(value: str | bytes) -> str:
    """Strict UTF-8 text normalized as NFC, casefold, then NFC.

    A full field above the explicit budget fails instead of being truncated.
    Exact types and encoding checks reject invalid Unicode before indexing or
    comparison; unpaired surrogates cannot be represented as UTF-8.
    """

    if type(value) is str and len(value) > MAX_FILE_BYTES:
        raise ValueError("Literal exceeds the input codepoint limit.")
    if _input_byte_size(value) > MAX_FILE_BYTES:
        raise ValueError("Literal exceeds the input byte limit.")
    if type(value) is bytes:
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Literal must be valid UTF-8.") from None
    elif isinstance(value, str):
        text = value
    else:
        raise TypeError("Literal must be str or exact bytes.")

    normalized = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    if len(normalized) > MAX_NORMALIZED_FIELD_BYTES:
        raise ValueError("Literal exceeds the normalized codepoint limit.")
    if len(normalized.encode("utf-8")) > MAX_NORMALIZED_FIELD_BYTES:
        raise ValueError("Literal exceeds the normalized byte limit.")
    return normalized


def normalize_keywords(keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Validate a bounded query and deduplicate whole normalized literals.

    An empty tuple is valid for a Tag-only or time-only query. Empty *items*
    are not valid. Budgets apply before deduplication so repeated input cannot
    evade request limits. Normal broad words are never limited by hit count.
    """

    if type(keywords) is not tuple:
        raise TypeError("Keywords must be an exact tuple.")
    if len(keywords) > MAX_QUERY_KEYWORDS:
        raise ValueError("Keyword count exceeds the query limit.")
    raw_bytes = 0
    normalized_bytes = 0
    seen: set[str] = set()
    result: list[str] = []
    for keyword in keywords:
        if type(keyword) is not str or not keyword:
            raise ValueError("Each keyword must be nonempty text.")
        try:
            raw_bytes += len(keyword.encode("utf-8", errors="strict"))
        except UnicodeError:
            raise ValueError("Keyword must be valid UTF-8.") from None
        if raw_bytes > MAX_QUERY_BYTES:
            raise ValueError("Keywords exceed the query byte limit.")
        normalized = normalize_literal(keyword)
        normalized_bytes += len(normalized.encode("utf-8"))
        if normalized_bytes > MAX_NORMALIZED_QUERY_BYTES:
            raise ValueError("Keywords exceed the normalized query byte limit.")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _encoded_gram(gram: str) -> str:
    # A letter prefix and hexadecimal UTF-8 bytes form one unicode61 token.
    # Neither the user's punctuation nor FTS5 operators enter MATCH syntax.
    return f"g{len(gram)}x{gram.encode('utf-8').hex()}"


def encoded_grams(value: str | bytes) -> tuple[str, ...]:
    """Unique, sorted 1/2/3-codepoint grams from one whole normalized field.

    Boundaries are Unicode codepoints, not words, Han runs, or files. Spaces,
    symbols, emoji, combining marks and NUL are ordinary literal content.
    Exceeding the distinct-term budget fails; no partial index is returned.
    """

    normalized = normalize_literal(value)
    raw_grams: set[str] = set()
    cached_chunks: set[str] = set()
    for start in range(0, len(normalized), _GRAM_CHUNK_CODEPOINTS):
        # Two following codepoints cover 2/3-grams starting at a chunk edge.
        window = normalized[start : start + _GRAM_CHUNK_CODEPOINTS + 2]
        if window in cached_chunks:
            continue
        if len(cached_chunks) < _MAX_CACHED_GRAM_CHUNKS:
            cached_chunks.add(window)
        start_count = min(_GRAM_CHUNK_CODEPOINTS, len(window))
        for offset in range(start_count):
            for width in range(1, min(3, len(window) - offset) + 1):
                gram = window[offset : offset + width]
                if gram not in raw_grams and len(raw_grams) >= MAX_UNIQUE_TERMS:
                    raise ValueError("Literal exceeds the unique-gram limit.")
                raw_grams.add(gram)
    return tuple(sorted(_encoded_gram(gram) for gram in raw_grams))


def candidate_match_expression(keywords: tuple[str, ...]) -> str:
    """Return an FTS5 OR expression containing only service-owned terms.

    One gram from each distinct literal is sufficient for a no-false-negative
    candidate superset when the index contains ``encoded_grams`` of every
    eligible complete field. A caller must bind this expression as data and
    then run exact per-field verification before returning any result. Empty
    keyword sets have no FTS expression (filter-only queries use another path).
    """

    normalized = normalize_keywords(keywords)
    if not normalized:
        raise ValueError("A filter-only query has no FTS expression.")
    tokens = {_encoded_gram(keyword[: min(3, len(keyword))]) for keyword in normalized}
    return " OR ".join(f'"{token}"' for token in sorted(tokens))


def find_page_hits(
    keywords: tuple[str, ...],
    *,
    title: str,
    body: str | bytes | None = None,
    text_files: tuple[tuple[str, str | bytes], ...] = (),
    opaque_file_names: tuple[str, ...] = (),
) -> tuple[LiteralHit, ...]:
    """Find all whole-keyword hits in independent, explicitly eligible fields.

    ``body`` is an optional legacy text body. Each declared text file
    contributes one filename and one complete text field; opaque files
    contribute names only. Tags and binary payloads are deliberately absent.
    The result has at most one hit per distinct keyword and field.
    """

    needles = normalize_keywords(keywords)
    if (
        type(title) is not str
        or type(text_files) is not tuple
        or type(opaque_file_names) is not tuple
    ):
        raise TypeError("Page fields have invalid types.")
    if len(text_files) + len(opaque_file_names) > MAX_FILES_PER_PAGE:
        raise ValueError("Page contains too many files.")

    hits: list[LiteralHit] = []
    seen_file_names: set[str] = set()
    total_bytes = 0
    total_work = 0

    def check_field(
        source: HitSource,
        file_name: str | None,
        value: str | bytes,
        *,
        count_page_bytes: bool = False,
    ) -> None:
        nonlocal total_bytes, total_work
        if count_page_bytes:
            total_bytes += _input_byte_size(value)
            if total_bytes > MAX_PAGE_BYTES:
                raise ValueError("Page exceeds the literal-match byte limit.")
        field = normalize_literal(value)
        total_work += len(field) * len(needles)
        if total_work > MAX_PAGE_MATCH_WORK:
            raise ValueError("Page and query exceed the literal-match work limit.")
        hits.extend(
            LiteralHit(keyword, source, file_name) for keyword in needles if keyword in field
        )

    check_field("title", None, title)
    if body is not None:
        check_field("body", None, body, count_page_bytes=True)
    for entry in text_files:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("Text files must be name and content pairs.")
        name, content = entry
        if type(name) is not str:
            raise TypeError("File names must be text.")
        name_key = normalize_literal(name)
        if name_key in seen_file_names:
            raise ValueError("Page contains duplicate file names.")
        seen_file_names.add(name_key)
        check_field("file_name", name, name)
        check_field("file_text", name, content, count_page_bytes=True)
    for name in opaque_file_names:
        if type(name) is not str:
            raise TypeError("File names must be text.")
        name_key = normalize_literal(name)
        if name_key in seen_file_names:
            raise ValueError("Page contains duplicate file names.")
        seen_file_names.add(name_key)
        check_field("file_name", name, name)

    return tuple(hits)


__all__ = [
    "GRAM_VERSION",
    "MAX_NORMALIZED_QUERY_BYTES",
    "MAX_QUERY_BYTES",
    "MAX_QUERY_KEYWORDS",
    "UNICODE_DATA_VERSION",
    "LiteralHit",
    "candidate_match_expression",
    "encoded_grams",
    "find_page_hits",
    "normalize_keywords",
    "normalize_literal",
]
