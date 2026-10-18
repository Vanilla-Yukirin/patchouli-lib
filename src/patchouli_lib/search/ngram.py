"""Bounded, deterministic literal text extraction for a future search index.

This module does not compile FTS expressions or SQL. Terms remain typed data;
future index adapters must encode and bind them safely after authorization.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Literal

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_NORMALIZED_BYTES = 4 * MAX_INPUT_BYTES
MAX_UNIQUE_TERMS = 262_144
NORMALIZATION_VERSION = "NFC-casefold-NFC-v1"
UNICODE_DATA_VERSION = unicodedata.unidata_version
HAN_RANGE_VERSION = "Unicode-15.1-plus-U+3007"

# Fixed Han classification rather than silently tracking the runtime's Unicode
# character categories. A future index must record the Unicode data version
# alongside NORMALIZATION_VERSION and rebuild if either changes.
_HAN_RANGES = (
    (0x3007, 0x3007),  # Ideographic number zero.
    (0x3400, 0x4DBF),  # Extension A.
    (0x4E00, 0x9FFF),  # Unified ideographs.
    (0xF900, 0xFAFF),  # Compatibility ideographs.
    (0x20000, 0x2A6DF),  # Extension B.
    (0x2A700, 0x2B73F),  # Extension C.
    (0x2B740, 0x2B81F),  # Extension D.
    (0x2B820, 0x2CEAF),  # Extension E.
    (0x2CEB0, 0x2EBEF),  # Extension F.
    (0x2EBF0, 0x2EE5F),  # Extension I.
    (0x2F800, 0x2FA1F),  # Compatibility supplement.
    (0x30000, 0x3134F),  # Extension G.
    (0x31350, 0x323AF),  # Extension H.
)
_HAN_STARTS = tuple(start for start, _end in _HAN_RANGES)

TermKind = Literal["han1", "han2", "han3", "word"]


@dataclass(frozen=True, slots=True)
class TermFrequency:
    """A literal normalized term and its overlapping occurrence count."""

    kind: TermKind
    text: str
    frequency: int


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    index = bisect_right(_HAN_STARTS, codepoint) - 1
    return index >= 0 and codepoint <= _HAN_RANGES[index][1]


def _normalize(value: str | bytes) -> str:
    if type(value) is bytes:
        if len(value) > MAX_INPUT_BYTES:
            raise ValueError("Input text exceeds the byte limit.")
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Input text must be valid UTF-8.") from None
    elif type(value) is str:
        # Reject an oversized Python string before allocating its UTF-8 copy.
        if len(value) > MAX_INPUT_BYTES:
            raise ValueError("Input text exceeds the codepoint limit.")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Input text must be valid UTF-8.") from None
        if len(encoded) > MAX_INPUT_BYTES:
            raise ValueError("Input text exceeds the byte limit.")
        text = value
    else:
        raise TypeError("Input text must be str or exact bytes.")

    normalized = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    if len(normalized) > MAX_NORMALIZED_BYTES:
        raise ValueError("Normalized text exceeds the codepoint limit.")
    if len(normalized.encode("utf-8")) > MAX_NORMALIZED_BYTES:
        raise ValueError("Normalized text exceeds the byte limit.")
    return normalized


def extract_term_frequencies(value: str | bytes) -> tuple[TermFrequency, ...]:
    """Return sorted, counted Han 1/2/3-grams and non-Han alphanumeric runs.

    Punctuation, whitespace and symbols separate runs and do not become terms.
    They are not parsed as SQL, FTS, boolean or wildcard syntax. Empty and
    punctuation-only text has no terms. Input is strictly UTF-8, with fixed
    raw/normalized byte and unique-term limits; overflow is an error, never a
    partial result.
    """

    normalized = _normalize(value)
    counts: Counter[tuple[TermKind, str]] = Counter()

    def add(kind: TermKind, term: str) -> None:
        key = (kind, term)
        if key not in counts and len(counts) >= MAX_UNIQUE_TERMS:
            raise ValueError("Text exceeds the unique-term limit.")
        counts[key] += 1

    index = 0
    while index < len(normalized):
        character = normalized[index]
        if _is_han(character):
            end = index + 1
            while end < len(normalized) and _is_han(normalized[end]):
                end += 1
            run = normalized[index:end]
            for width in range(1, min(3, len(run)) + 1):
                kind: TermKind = ("han1", "han2", "han3")[width - 1]
                for offset in range(len(run) - width + 1):
                    add(kind, run[offset : offset + width])
            index = end
            continue

        if character.isalnum():
            end = index + 1
            while end < len(normalized) and not _is_han(normalized[end]):
                following = normalized[end]
                if not (following.isalnum() or unicodedata.category(following).startswith("M")):
                    break
                end += 1
            add("word", normalized[index:end])
            index = end
            continue

        index += 1

    return tuple(
        TermFrequency(kind, term, frequency) for (kind, term), frequency in sorted(counts.items())
    )


__all__ = [
    "HAN_RANGE_VERSION",
    "MAX_INPUT_BYTES",
    "MAX_NORMALIZED_BYTES",
    "MAX_UNIQUE_TERMS",
    "NORMALIZATION_VERSION",
    "UNICODE_DATA_VERSION",
    "TermFrequency",
    "extract_term_frequencies",
]
