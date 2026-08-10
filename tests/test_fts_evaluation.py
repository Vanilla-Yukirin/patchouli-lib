from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_SCRIPT = REPOSITORY_ROOT / "scripts" / "evaluate_fts.py"


def _run_evaluation(output_format: str) -> str:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(EVALUATION_SCRIPT), "--format", output_format],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result.stdout


def _candidate(report: dict[str, object], name: str) -> dict[str, object]:
    candidates = cast(list[dict[str, object]], report["candidates"])
    return next(candidate for candidate in candidates if candidate["name"] == name)


def _query(candidate: dict[str, object], query_id: str) -> dict[str, object]:
    queries = cast(list[dict[str, object]], candidate["query_results"])
    return next(query for query in queries if query["query_id"] == query_id)


def _hit_ids(query: dict[str, object]) -> list[str]:
    hits = cast(list[dict[str, object]], query["hits"])
    return [cast(str, hit["document_id"]) for hit in hits]


@pytest.fixture(scope="module")
def evaluation_report() -> dict[str, object]:
    parsed = json.loads(_run_evaluation("json"))
    return cast(dict[str, object], parsed)


def test_report_records_runtime_and_original_fixture_provenance(
    evaluation_report: dict[str, object],
) -> None:
    fixture = cast(dict[str, object], evaluation_report["fixture"])

    assert evaluation_report["python_version"] == platform.python_version()
    assert evaluation_report["unicode_database_version"] == unicodedata.unidata_version
    assert fixture["provenance"] == (
        "Original synthetic text authored for the PatchouliLib evaluation; "
        "not adapted from an external corpus."
    )


def test_known_han_recall_boundaries(evaluation_report: dict[str, object]) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    unicode_recall = cast(dict[str, float], unicode61["recall_by_dimension"])
    trigram_recall = cast(dict[str, float], trigram["recall_by_dimension"])
    application_recall = cast(dict[str, float], application["recall_by_dimension"])

    assert unicode_recall["han_1"] == 0.0
    assert unicode_recall["han_2"] == 0.0
    assert unicode_recall["han_3_plus"] == 0.0
    assert trigram_recall["han_1"] == 0.0
    assert trigram_recall["han_2"] == 0.0
    assert trigram_recall["han_3_plus"] == 1.0
    assert application_recall["han_1"] == 1.0
    assert application_recall["han_2"] == 1.0
    assert application_recall["han_3_plus"] == 1.0
    assert application_recall["mixed_language"] == 1.0


def test_candidates_replace_old_current_content_and_rank_deterministically(
    evaluation_report: dict[str, object],
) -> None:
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    for candidate in candidates:
        current_only = cast(dict[str, bool], candidate["current_only_update"])
        assert current_only == {
            "legacy_found_before_update": True,
            "legacy_absent_after_update": True,
            "current_found_after_update": True,
            "passed": True,
        }
        assert candidate["deterministic_ranking"] is True


def test_plain_text_compilers_do_not_expose_caller_fts_syntax(
    evaluation_report: dict[str, object],
) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    assert _query(unicode61, "literal-and")["compiled_query"] == '"AND"'
    assert _query(unicode61, "literal-quotes")["compiled_query"] == '"""quoted"""'
    assert _query(unicode61, "literal-wildcard")["compiled_query"] == '"prefix*"'
    assert _query(unicode61, "literal-column-selector")["compiled_query"] == '"title:demo"'
    assert _query(unicode61, "literal-unmatched-quote")["compiled_query"] == '"unmatched""quote"'
    assert _query(unicode61, "literal-or")["compiled_query"] == '"alpha OR beta"'
    assert _query(unicode61, "literal-near-parentheses")["compiled_query"] == '"NEAR(alpha beta)"'
    assert (
        _query(unicode61, "literal-grouped-or-near")["compiled_query"]
        == '"(alpha OR beta) NEAR(gamma delta)"'
    )

    for query_id in (
        "literal-wildcard",
        "literal-unmatched-quote",
        "literal-or",
        "literal-near-parentheses",
        "literal-grouped-or-near",
    ):
        compiled = cast(str, _query(application, query_id)["compiled_query"])
        assert "prefix" not in compiled
        assert "unmatched" not in compiled
        assert "OR" not in compiled
        assert "NEAR" not in compiled
        assert "*" not in compiled
        assert "(" not in compiled
        assert ")" not in compiled
        assert all(part.startswith('"') and part.endswith('"') for part in compiled.split(" AND "))


def test_literal_operator_combinations_do_not_expand_match_grammar(
    evaluation_report: dict[str, object],
) -> None:
    candidates = cast(list[dict[str, object]], evaluation_report["candidates"])

    for candidate in candidates:
        for query_id in (
            "literal-unmatched-quote",
            "literal-or",
            "literal-near-parentheses",
            "literal-grouped-or-near",
        ):
            assert _hit_ids(_query(candidate, query_id)) == ["literal-syntax"]


def test_literal_wildcard_case_records_precision_limit(
    evaluation_report: dict[str, object],
) -> None:
    unicode61 = _candidate(evaluation_report, "unicode61")
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    assert _query(unicode61, "literal-wildcard")["recall"] == 1.0
    assert _query(unicode61, "literal-wildcard")["precision"] == 0.5
    assert _query(trigram, "literal-wildcard")["precision"] == 1.0
    assert _query(application, "literal-wildcard")["precision"] == 0.5


def test_tokenized_candidate_does_not_claim_native_original_snippets(
    evaluation_report: dict[str, object],
) -> None:
    trigram = _candidate(evaluation_report, "trigram")
    application = _candidate(evaluation_report, "application_cjk_ngrams")

    trigram_hits = cast(list[dict[str, object]], _query(trigram, "han-3")["hits"])
    application_hits = cast(list[dict[str, object]], _query(application, "han-3")["hits"])
    assert trigram_hits[0]["native_snippet"]
    assert application["native_snippet_supported"] is False
    assert application_hits[0]["native_snippet"] is None


def test_markdown_report_states_evaluation_boundary() -> None:
    rendered = _run_evaluation("markdown")

    assert "| `unicode61`" in rendered
    assert "| `trigram`" in rendered
    assert "| `application_cjk_ngrams`" in rendered
    assert "does not select a production tokenizer" in rendered
