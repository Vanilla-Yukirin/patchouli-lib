from __future__ import annotations

import unicodedata

import pytest

from patchouli_lib.search import TermFrequency, extract_term_frequencies, ngram


def test_overlapping_han_unigrams_bigrams_trigrams_keep_frequency() -> None:
    assert extract_term_frequencies("哈哈哈") == (
        TermFrequency("han1", "哈", 3),
        TermFrequency("han2", "哈哈", 2),
        TermFrequency("han3", "哈哈哈", 1),
    )


def test_punctuation_whitespace_and_script_changes_separate_runs() -> None:
    terms = extract_term_frequencies("中文，文档\nABC-123;中A文")
    assert terms == (
        TermFrequency("han1", "中", 2),
        TermFrequency("han1", "文", 3),
        TermFrequency("han1", "档", 1),
        TermFrequency("han2", "中文", 1),
        TermFrequency("han2", "文档", 1),
        TermFrequency("word", "123", 1),
        TermFrequency("word", "a", 1),
        TermFrequency("word", "abc", 1),
    )
    assert extract_term_frequencies(" \n,.;!? ") == ()


def test_nfc_casefold_and_strict_utf8_are_predictable() -> None:
    assert unicodedata.unidata_version == ngram.UNICODE_DATA_VERSION
    text = "Straße STRASSE café cafe\u0301 İ i ＡＢＣ ABC123"
    assert extract_term_frequencies(text.encode("utf-8")) == extract_term_frequencies(text)
    assert extract_term_frequencies(text) == (
        TermFrequency("word", "abc123", 1),
        TermFrequency("word", "café", 2),
        TermFrequency("word", "i", 1),
        TermFrequency("word", "i\u0307", 1),
        TermFrequency("word", "strasse", 2),
        TermFrequency("word", "ａｂｃ", 1),
    )


def test_fixed_han_ranges_include_supplementary_ideographs_and_ideographic_zero() -> None:
    extension_i = "\U0002ebf0"
    assert TermFrequency("han2", f"{extension_i}〇", 1) in extract_term_frequencies(
        f"{extension_i}〇"
    )


def test_sql_and_fts_looking_text_remains_literal_data() -> None:
    terms = extract_term_frequencies("foo' OR 1=1 -- NEAR(bar)*")
    assert terms == (
        TermFrequency("word", "1", 2),
        TermFrequency("word", "bar", 1),
        TermFrequency("word", "foo", 1),
        TermFrequency("word", "near", 1),
        TermFrequency("word", "or", 1),
    )
    assert terms == extract_term_frequencies("foo' OR 1=1 -- NEAR(bar)*")


@pytest.mark.parametrize("value", [b"\xff", b"\xc0\xaf", "\ud800"])
def test_invalid_utf8_is_rejected(value: str | bytes) -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        extract_term_frequencies(value)


def test_unsupported_input_type_and_oversized_raw_input_are_rejected() -> None:
    with pytest.raises(TypeError, match="str or exact bytes"):
        extract_term_frequencies(bytearray(b"abc"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="byte limit"):
        extract_term_frequencies(b"x" * (ngram.MAX_INPUT_BYTES + 1))
    with pytest.raises(ValueError, match="codepoint limit"):
        extract_term_frequencies("x" * (ngram.MAX_INPUT_BYTES + 1))


def test_unique_term_overflow_fails_instead_of_returning_partial_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ngram, "MAX_UNIQUE_TERMS", 2)
    with pytest.raises(ValueError, match="unique-term limit"):
        extract_term_frequencies("中文")
