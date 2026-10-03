"""Pure, offline-only projection checks; these do not activate search."""

from __future__ import annotations

import pytest

from patchouli_lib.search.ngram import MAX_INPUT_BYTES
from patchouli_lib.search.projection_v2 import (
    MAX_EXPERIMENT_KEYWORDS,
    MAX_EXPERIMENT_QUERY_BYTES,
    candidate_match_expression,
    normalize_literal,
    project_page_text,
)


def _one_candidate(keyword: str) -> str:
    expression = candidate_match_expression((keyword,))
    assert expression.startswith('"') and expression.endswith('"')
    return expression[1:-1]


def test_explicit_current_text_and_opaque_names_project_without_binary_bytes() -> None:
    projected = project_page_text(
        title="Straße 资料",
        body="正文🙂!?".encode(),
        text_files=(("notes.md", "附页 tech-报告".encode()),),
        opaque_file_names=("binary-needle.bin",),
    )
    assert normalize_literal("Straße") == "strasse"
    assert _one_candidate("STRASSE") in projected.title_terms.split()
    assert _one_candidate("正文") in projected.body_terms.split()
    assert _one_candidate("🙂!?") in projected.body_terms.split()
    assert _one_candidate("tech-") in projected.file_terms.split()
    assert _one_candidate("binary-needle.bin") in projected.file_terms.split()
    assert _one_candidate("二进制隐匿密文") not in projected.file_terms.split()


def test_binary_only_page_contributes_title_and_name_but_no_body() -> None:
    projected = project_page_text(
        title="视频摘要",
        body=None,
        opaque_file_names=("clip.mp4",),
    )
    assert projected.body_terms == ""
    assert _one_candidate("视频") in projected.title_terms.split()
    assert _one_candidate("clip") in projected.file_terms.split()


def test_file_boundaries_do_not_create_spurious_cross_file_grams() -> None:
    projected = project_page_text(
        title="Sample",
        body=None,
        text_files=(("first.txt", "abc"), ("second.txt", "def")),
    )
    assert _one_candidate("abc") in projected.file_terms.split()
    assert _one_candidate("def") in projected.file_terms.split()
    assert _one_candidate("cde") not in projected.file_terms.split()


def test_invalid_or_oversized_declared_text_fails_instead_of_truncating() -> None:
    with pytest.raises(ValueError):
        normalize_literal(b"\xff")
    with pytest.raises(ValueError):
        project_page_text(title="x", body=b"\xff")
    with pytest.raises(ValueError):
        project_page_text(title="x", body="x" * MAX_INPUT_BYTES)
    with pytest.raises(ValueError):
        project_page_text(title="x", body=None, text_files=(("notes.md", b"\xff"),))
    with pytest.raises(TypeError):
        project_page_text(title="x", body=None, text_files=(("notes.md", 7),))  # type: ignore[arg-type]


def test_file_name_and_count_ambiguity_is_rejected() -> None:
    with pytest.raises(ValueError):
        project_page_text(
            title="x",
            body=None,
            text_files=(("README.md", "a"),),
            opaque_file_names=("readme.md",),
        )
    with pytest.raises(ValueError):
        project_page_text(title="x", body=None, opaque_file_names=("../escape.txt",))
    with pytest.raises(ValueError):
        project_page_text(
            title="x",
            body=None,
            opaque_file_names=tuple(f"file-{number}.bin" for number in range(65)),
        )


def test_experiment_query_is_bounded_and_never_compiles_caller_fts_syntax() -> None:
    expression = candidate_match_expression(('NEAR("secret") OR title:admin*',))
    assert "NEAR" not in expression
    assert "title:" not in expression
    assert "*" not in expression
    assert candidate_match_expression(("Straße", "STRASSE")) == candidate_match_expression(
        ("Straße",)
    )
    with pytest.raises(ValueError):
        candidate_match_expression(())
    with pytest.raises(ValueError):
        candidate_match_expression(("",))
    with pytest.raises(ValueError):
        candidate_match_expression(("x",) * (MAX_EXPERIMENT_KEYWORDS + 1))
    with pytest.raises(ValueError):
        candidate_match_expression(("x" * (MAX_EXPERIMENT_QUERY_BYTES + 1),))
