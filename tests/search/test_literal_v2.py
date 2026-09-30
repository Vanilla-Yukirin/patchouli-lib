"""Pure literal-search checks; no production index or search route is enabled."""

from __future__ import annotations

import itertools
import re
import sqlite3
import unicodedata

import pytest

from patchouli_lib.content.file_manifest import MAX_FILE_BYTES
from patchouli_lib.search.literal_v2 import (
    MAX_QUERY_BYTES,
    MAX_QUERY_KEYWORDS,
    LiteralHit,
    candidate_match_expression,
    encoded_grams,
    encoded_library_token,
    find_page_hits,
    normalize_keywords,
    normalize_literal,
)
from patchouli_lib.search.ngram import MAX_UNIQUE_TERMS


def _oracle(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold())


def test_normalization_and_query_deduplication() -> None:
    assert normalize_literal("e\u0301 Straße") == "é strasse"
    assert normalize_literal("Ａ") == "ａ"  # NFC is not NFKC.
    assert normalize_keywords(("Straße", "STRASSE", "é", "e\u0301")) == ("strasse", "é")
    assert normalize_keywords(()) == ()
    assert normalize_keywords((" ",)) == (" ",)


def test_library_posting_token_is_stable_and_separate_from_text_grams() -> None:
    first = encoded_library_token("library-one")
    assert first == encoded_library_token("library-one")
    assert first != encoded_library_token("library-two")
    assert re.fullmatch(r"libx[0-9a-f]{32}", first)
    assert first not in encoded_grams("library-one")
    with pytest.raises(ValueError):
        encoded_library_token("")


def test_every_unicode_codepoint_and_boundary_can_be_indexed() -> None:
    normalized = _oracle("A中!?👩\u200d💻 e\u0301\x00")
    grams = set(encoded_grams("A中!?👩\u200d💻 e\u0301\x00"))
    for width in (1, 2, 3):
        for offset in range(len(normalized) - width + 1):
            fragment = normalized[offset : offset + width]
            assert f"g{width}x{fragment.encode('utf-8').hex()}" in grams
    assert len(grams) == len(set(grams))
    assert all(re.fullmatch(r"g[123]x[0-9a-f]+", gram) for gram in grams)
    assert encoded_grams("") == ()


def test_grams_across_internal_chunk_edges_are_not_lost() -> None:
    field = "a" * 4095 + "🙂!?" + "a" * 4096
    terms = set(encoded_grams(field))
    for fragment in ("a🙂!", "🙂!?", "!?a"):
        assert candidate_match_expression((fragment,)).strip('"') in terms


def test_candidate_is_a_superset_for_exhaustive_small_literals() -> None:
    alphabet = ("a", "中", "!", "🙂", " ")
    fields = [
        "".join(characters)
        for width in range(5)
        for characters in itertools.product(alphabet, repeat=width)
    ]
    keywords = [
        "".join(characters)
        for width in range(1, 4)
        for characters in itertools.product(alphabet, repeat=width)
    ]
    for field in fields:
        indexed = set(encoded_grams(field))
        normalized_field = _oracle(field)
        for keyword in keywords:
            if _oracle(keyword) in normalized_field:
                assert candidate_match_expression((keyword,)).strip('"') in indexed


def test_complete_short_gram_is_exact_for_a_single_normalized_field() -> None:
    fields = ("", "技术", "技 x 术", "a\x00🙂!", "Straße", "e\u0301", "ab", "🙂🙂")
    keywords = ("技", "技术", "术", "技 x", "\x00", "\x00🙂", "🙂!", "ß", "SS", "é")
    for field in fields:
        indexed = set(encoded_grams(field))
        normalized_field = normalize_literal(field)
        for keyword in keywords:
            normalized_keyword = normalize_literal(keyword)
            if len(normalized_keyword) > 3:
                continue
            gram = candidate_match_expression((keyword,)).strip('"')
            assert (gram in indexed) == (normalized_keyword in normalized_field)


def test_fts5_receives_only_bound_generated_terms_and_needs_exact_recheck() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE candidate USING fts5(terms)")
        pages = (
            (1, "A中!?👩\u200d💻"),
            (2, "abcdef"),
            (3, "safe text"),
        )
        for page_id, field in pages:
            connection.execute(
                "INSERT INTO candidate(rowid, terms) VALUES (?, ?)",
                (page_id, " ".join(encoded_grams(field))),
            )
        connection.execute(
            "INSERT INTO candidate(rowid, terms) VALUES (?, ?)",
            (4, " ".join((*encoded_grams("abc"), *encoded_grams("def")))),
        )

        for keyword, expected in (("中!?", 1), ("👩\u200d💻", 1), ("bcd", 2), (" ", 3)):
            expression = candidate_match_expression((keyword,))
            assert re.fullmatch(r'"g[123]x[0-9a-f]+"', expression)
            rows = connection.execute(
                "SELECT rowid FROM candidate WHERE terms MATCH ?", (expression,)
            ).fetchall()
            assert (expected,) in rows

        cross_file_candidate = connection.execute(
            "SELECT rowid FROM candidate WHERE terms MATCH ?",
            (candidate_match_expression(("abcdef",)),),
        ).fetchall()
        assert (4,) in cross_file_candidate
        assert not find_page_hits(
            ("abcdef",), title="x", text_files=(("first", "abc"), ("second", "def"))
        )

        attack = '" OR title:secret* NEAR(x)'
        expression = candidate_match_expression((attack, "🙂"))
        assert re.fullmatch(r'"g[123]x[0-9a-f]+"(?: OR "g[123]x[0-9a-f]+")*', expression)
        assert '" OR title:' not in expression
        connection.execute(
            "SELECT rowid FROM candidate WHERE terms MATCH ?", (expression,)
        ).fetchall()
    finally:
        connection.close()


def test_keywords_are_or_and_hits_never_span_fields_or_files() -> None:
    hits = find_page_hits(
        ("STRASSE", "strasse", "🙂!", "cde", "secret", "notes", "opaque"),
        title="Straße",
        body="🙂!",
        text_files=(("notes.md", "abc"), ("other.txt", "def")),
        opaque_file_names=("opaque.bin",),
    )
    assert hits == (
        LiteralHit("strasse", "title"),
        LiteralHit("🙂!", "body"),
        LiteralHit("notes", "file_name", "notes.md"),
        LiteralHit("opaque", "file_name", "opaque.bin"),
    )
    assert not find_page_hits(("abcdef",), title="abc", body="def")
    assert not find_page_hits(("cde",), title="x", text_files=(("a", "abc"), ("b", "def")))
    assert not find_page_hits(("secret",), title="x", opaque_file_names=("document.bin",))


def test_multiple_sources_can_report_the_same_keyword_without_tag_matching() -> None:
    assert find_page_hits(
        ("i",),
        title="I",
        body="i",
        text_files=(("i.txt", "fi"),),
    ) == (
        LiteralHit("i", "title"),
        LiteralHit("i", "body"),
        LiteralHit("i", "file_name", "i.txt"),
        LiteralHit("i", "file_text", "i.txt"),
    )
    assert not find_page_hits(("tag-name",), title="unrelated")


def test_invalid_or_oversized_input_fails_without_partial_results() -> None:
    with pytest.raises(ValueError):
        normalize_literal(b"\xff")
    with pytest.raises(ValueError):
        normalize_literal("\ud800")
    with pytest.raises(TypeError):
        normalize_literal(7)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        normalize_keywords(("",))
    with pytest.raises(ValueError):
        normalize_keywords(("\ud800",))
    with pytest.raises(ValueError):
        normalize_keywords(("x",) * (MAX_QUERY_KEYWORDS + 1))
    with pytest.raises(ValueError):
        normalize_keywords(("x" * (MAX_QUERY_BYTES + 1),))
    with pytest.raises(ValueError):
        candidate_match_expression(())
    with pytest.raises(ValueError):
        encoded_grams("x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="unique-gram limit"):
        encoded_grams("".join(chr(0x10000 + index) for index in range(MAX_UNIQUE_TERMS + 1)))
    with pytest.raises(ValueError):
        find_page_hits(("x",), title="x", text_files=(("name", b"\xff"),))
    with pytest.raises(ValueError, match="duplicate file names"):
        find_page_hits(("x",), title="x", opaque_file_names=("Case.txt", "case.txt"))


def test_query_bound_applies_before_deduplication() -> None:
    repeated = ("a" * (MAX_QUERY_BYTES // 2 + 1),) * 2
    with pytest.raises(ValueError, match="query byte limit"):
        normalize_keywords(repeated)


def test_legal_maximum_file_and_page_are_not_truncated() -> None:
    full_file = "a" * MAX_FILE_BYTES
    assert set(encoded_grams(full_file)) == {"g1x61", "g2x6161", "g3x616161"}
    hits = find_page_hits(
        ("aaa",),
        title="index",
        text_files=tuple((f"file-{number}.txt", full_file) for number in range(4)),
    )
    assert len(hits) == 4
    assert all(hit.source == "file_text" for hit in hits)


def test_excessive_page_query_work_fails_explicitly() -> None:
    full_file = "a" * MAX_FILE_BYTES
    many_distinct_keywords = tuple(f"a{index}" for index in range(33))
    with pytest.raises(ValueError, match="work limit"):
        find_page_hits(many_distinct_keywords, title="x", body=full_file)
