"""Pure text projection for the offline search-v2 candidate.

This module neither reads a database nor decides which Page files are text.
Callers must explicitly identify UTF-8 text files; opaque files contribute only
their names. The rules are experimental while the public search proposal is
Proposed and must not be interpreted as an enabled production search contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from patchouli_lib.content.file_manifest import MAX_FILES_PER_PAGE, normalize_file_name
from patchouli_lib.search.ngram import (
    MAX_INPUT_BYTES,
    MAX_UNIQUE_TERMS,
    _is_han,
    _normalize,
    extract_term_frequencies,
)

SHORT_LITERAL_TERMS_VERSION = "non-Han-codepoint-1-2-3-NFC-casefold-NFC-v1"
PROJECTION_VERSION = "offline-literal-page-projection-v1"
MAX_INDEXABLE_PAGE_TEXT_BYTES = MAX_INPUT_BYTES
MAX_EXPERIMENT_QUERY_BYTES = 32 * 1024
MAX_EXPERIMENT_KEYWORDS = 256


@dataclass(frozen=True, slots=True)
class LiteralPageProjection:
    """Generated FTS columns for one Page's explicit current text and names."""

    title_terms: str
    body_terms: str
    file_terms: str


def normalize_literal(value: str | bytes) -> str:
    """Normalize one complete UTF-8 literal, rejecting invalid or oversized text."""

    return _normalize(value)


def _non_han_short_terms(value: str) -> set[str]:
    """Encode 1/2/3-codepoint grams within each non-Han run, including symbols."""

    normalized = normalize_literal(value)
    terms: set[str] = set()
    start = 0
    while start < len(normalized):
        if _is_han(normalized[start]):
            start += 1
            continue
        end = start + 1
        while end < len(normalized) and not _is_han(normalized[end]):
            end += 1
        run = normalized[start:end]
        for width in range(1, min(3, len(run)) + 1):
            for offset in range(len(run) - width + 1):
                token = f"c{width}{run[offset : offset + width].encode('utf-8').hex()}"
                if token not in terms and len(terms) >= MAX_UNIQUE_TERMS:
                    raise ValueError("Text exceeds the unique-term limit.")
                terms.add(token)
        start = end
    return terms


def _encoded_terms(value: str) -> set[str]:
    terms = {
        f"{term.kind}{term.text.encode('utf-8').hex()}"
        for term in extract_term_frequencies(value)
        if term.kind != "word"
    }
    terms.update(_non_han_short_terms(value))
    if len(terms) > MAX_UNIQUE_TERMS:
        raise ValueError("Text exceeds the unique-term limit.")
    return terms


def _validated_file_name(name: str, seen: set[str]) -> str:
    normalized = normalize_file_name(name)
    if normalized != name:
        raise ValueError("File name must already be normalized.")
    key = normalize_literal(name).casefold()
    if key in seen:
        raise ValueError("Page contains duplicate or ambiguous file names.")
    seen.add(key)
    return name


def project_page_text(
    *,
    title: str,
    body: str | bytes | None,
    text_files: tuple[tuple[str, str | bytes], ...] = (),
    opaque_file_names: tuple[str, ...] = (),
) -> LiteralPageProjection:
    """Project explicitly labelled text, with no MIME inference or partial index.

    ``body`` is the optional legacy Markdown body in this experiment. A
    binary-only Page passes ``None``. Each entry in ``text_files`` must be a
    complete UTF-8 text value; an opaque file is represented by its name only.
    No file payload, old Revision or deleted Page is discovered by this function.
    Its aggregate text budget is intentionally smaller than the Page storage
    limit, so a caller must fail closed rather than pretend a large Page was
    completely indexed.
    """

    if type(title) is not str or not title:
        raise ValueError("Page title must be nonempty text.")
    if type(text_files) is not tuple or type(opaque_file_names) is not tuple:
        raise TypeError("File lists must be exact tuples.")
    if len(text_files) + len(opaque_file_names) > MAX_FILES_PER_PAGE:
        raise ValueError("Page contains too many files.")

    normalized_title = normalize_literal(title)
    normalized_body = "" if body is None else normalize_literal(body)
    total_bytes = len(normalized_title.encode("utf-8")) + len(normalized_body.encode("utf-8"))
    if total_bytes > MAX_INDEXABLE_PAGE_TEXT_BYTES:
        raise ValueError("Page exceeds the experimental indexable-text budget.")

    seen_names: set[str] = set()
    file_terms: set[str] = set()
    for entry in text_files:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("Text files must be name and UTF-8 content pairs.")
        name, content = entry
        _validated_file_name(name, seen_names)
        normalized_content = normalize_literal(content)
        total_bytes += len(name.encode("utf-8")) + len(normalized_content.encode("utf-8"))
        if total_bytes > MAX_INDEXABLE_PAGE_TEXT_BYTES:
            raise ValueError("Page exceeds the experimental indexable-text budget.")
        file_terms.update(_encoded_terms(name))
        file_terms.update(_encoded_terms(normalized_content))
        if len(file_terms) > MAX_UNIQUE_TERMS:
            raise ValueError("Page exceeds the unique-term limit.")
    for name in opaque_file_names:
        _validated_file_name(name, seen_names)
        total_bytes += len(name.encode("utf-8"))
        if total_bytes > MAX_INDEXABLE_PAGE_TEXT_BYTES:
            raise ValueError("Page exceeds the experimental indexable-text budget.")
        file_terms.update(_encoded_terms(name))
        if len(file_terms) > MAX_UNIQUE_TERMS:
            raise ValueError("Page exceeds the unique-term limit.")

    title_terms = _encoded_terms(normalized_title)
    body_terms = _encoded_terms(normalized_body)
    if len(title_terms | body_terms | file_terms) > MAX_UNIQUE_TERMS:
        raise ValueError("Page exceeds the unique-term limit.")
    return LiteralPageProjection(
        title_terms=" ".join(sorted(title_terms)),
        body_terms=" ".join(sorted(body_terms)),
        file_terms=" ".join(sorted(file_terms)),
    )


def candidate_match_expression(keywords: tuple[str, ...]) -> str:
    """Generate only safe, service-owned FTS tokens for experimental recall.

    One gram per literal gives a superset, never a complete match decision.
    The caller must check each whole normalized literal in one authorized field
    before ranking or returning a Page.
    """

    if type(keywords) is not tuple or not 1 <= len(keywords) <= MAX_EXPERIMENT_KEYWORDS:
        raise ValueError("Keyword count exceeds the experimental query budget.")
    total_bytes = 0
    tokens: set[str] = set()
    for keyword in keywords:
        if type(keyword) is not str or not keyword:
            raise ValueError("Each keyword must be nonempty text.")
        total_bytes += len(keyword.encode("utf-8", errors="strict"))
        if total_bytes > MAX_EXPERIMENT_QUERY_BYTES:
            raise ValueError("Keywords exceed the experimental query byte budget.")
        han_terms = [
            term
            for term in extract_term_frequencies(keyword)
            if term.kind in {"han1", "han2", "han3"}
        ]
        if han_terms:
            chosen = max(han_terms, key=lambda term: (int(term.kind[-1]), term.text))
            tokens.add(f"{chosen.kind}{chosen.text.encode('utf-8').hex()}")
        else:
            short_terms = _non_han_short_terms(keyword)
            if not short_terms:
                raise ValueError("Keyword has no indexable literal terms.")
            tokens.add(max(short_terms, key=lambda token: (int(token[1]), token)))
    return " OR ".join(f'"{token}"' for token in sorted(tokens))


__all__ = [
    "MAX_EXPERIMENT_KEYWORDS",
    "MAX_EXPERIMENT_QUERY_BYTES",
    "MAX_INDEXABLE_PAGE_TEXT_BYTES",
    "PROJECTION_VERSION",
    "SHORT_LITERAL_TERMS_VERSION",
    "LiteralPageProjection",
    "candidate_match_expression",
    "normalize_literal",
    "project_page_text",
]
