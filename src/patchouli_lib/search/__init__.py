"""Pure search-text preparation primitives; no index or public search API."""

from patchouli_lib.search.ngram import (
    HAN_RANGE_VERSION,
    MAX_INPUT_BYTES,
    MAX_UNIQUE_TERMS,
    NORMALIZATION_VERSION,
    UNICODE_DATA_VERSION,
    TermFrequency,
    extract_term_frequencies,
)

__all__ = [
    "HAN_RANGE_VERSION",
    "MAX_INPUT_BYTES",
    "MAX_UNIQUE_TERMS",
    "NORMALIZATION_VERSION",
    "UNICODE_DATA_VERSION",
    "TermFrequency",
    "extract_term_frequencies",
]
