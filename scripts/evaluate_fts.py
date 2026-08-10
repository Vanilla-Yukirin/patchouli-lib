from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "retrieval" / "fts_corpus.json"

CandidateName = Literal["unicode61", "trigram", "application_cjk_ngrams"]


@dataclass(frozen=True)
class Document:
    document_id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class QueryCase:
    query_id: str
    dimension: str
    text: str
    relevant: tuple[str, ...]


@dataclass(frozen=True)
class Corpus:
    description: str
    license: str
    provenance: str
    documents: tuple[Document, ...]
    queries: tuple[QueryCase, ...]


@dataclass(frozen=True)
class Candidate:
    name: CandidateName
    sqlite_tokenizer: str
    application_tokenized: bool
    description: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    score: float
    native_snippet: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "score": self.score,
            "native_snippet": self.native_snippet,
        }


@dataclass(frozen=True)
class QueryEvaluation:
    query_id: str
    dimension: str
    text: str
    compiled_query: str
    relevant: tuple[str, ...]
    hits: tuple[SearchHit, ...]
    recall: float
    precision: float
    deterministic: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "dimension": self.dimension,
            "text": self.text,
            "compiled_query": self.compiled_query,
            "relevant": list(self.relevant),
            "hits": [hit.as_dict() for hit in self.hits],
            "recall": self.recall,
            "precision": self.precision,
            "deterministic": self.deterministic,
        }


CANDIDATES = (
    Candidate(
        name="unicode61",
        sqlite_tokenizer="unicode61",
        application_tokenized=False,
        description=(
            "FTS5 unicode61 over original title, summary, tags, and body. Caller text is "
            "compiled as one quoted literal phrase."
        ),
        limitations=(
            "Contiguous Han text is commonly indexed as one long token, so short Han queries "
            "inside that run are not recalled.",
            "Native FTS snippets are available, but highlights follow unicode61 token boundaries.",
        ),
    ),
    Candidate(
        name="trigram",
        sqlite_tokenizer="trigram",
        application_tokenized=False,
        description=(
            "FTS5 trigram over original title, summary, tags, and body. Caller text is compiled "
            "as one quoted literal phrase."
        ),
        limitations=(
            "MATCH terms shorter than three Unicode code points do not provide useful recall.",
            "Substring matches can increase false positives and make term-oriented ranking noisy.",
            "Native snippets can highlight broad overlapping regions rather than linguistic words.",
        ),
    ),
    Candidate(
        name="application_cjk_ngrams",
        sqlite_tokenizer="unicode61",
        application_tokenized=True,
        description=(
            "Application-normalized index using NFKC plus case-folding. Han runs emit encoded "
            "overlapping one-, two-, and three-character grams; other alphanumeric runs emit "
            "encoded word tokens. The encoded tokens are stored in a unicode61 FTS table."
        ),
        limitations=(
            "The index expands each Han run and therefore costs more storage and indexing work.",
            "AND over overlapping grams approximates substring matching but is not linguistic "
            "segmentation and can produce false positives when grams occur in different places.",
            "Punctuation is treated as a separator, so punctuation-sensitive literal searches "
            "can match an unpunctuated decoy unless a separate verification step is added.",
            "Native FTS snippets contain encoded tokens, so production would need a separate, "
            "bounded snippet generator over the original text with explicit offset semantics.",
        ),
    ),
)


def _as_object_map(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{context} keys must be strings")
    return {cast(str, key): item for key, item in raw.items()}


def _as_object_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    return cast(list[object], value)


def _required_string(record: dict[str, object], key: str, context: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _string_tuple(record: dict[str, object], key: str, context: str) -> tuple[str, ...]:
    values = _as_object_list(record.get(key), f"{context}.{key}")
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{context}.{key} must contain non-empty strings")
    return tuple(cast(str, value) for value in values)


def load_corpus(path: Path = DEFAULT_FIXTURE) -> Corpus:
    payload = _as_object_map(json.loads(path.read_text(encoding="utf-8")), "fixture")
    if payload.get("schema_version") != 1:
        raise ValueError("fixture.schema_version must be 1")

    documents: list[Document] = []
    seen_document_ids: set[str] = set()
    for index, item in enumerate(_as_object_list(payload.get("documents"), "documents")):
        context = f"documents[{index}]"
        record = _as_object_map(item, context)
        document_id = _required_string(record, "id", context)
        if document_id in seen_document_ids:
            raise ValueError(f"duplicate document id: {document_id}")
        seen_document_ids.add(document_id)
        documents.append(
            Document(
                document_id=document_id,
                title=_required_string(record, "title", context),
                summary=_required_string(record, "summary", context),
                tags=_string_tuple(record, "tags", context),
                body=_required_string(record, "body", context),
            )
        )

    queries: list[QueryCase] = []
    seen_query_ids: set[str] = set()
    for index, item in enumerate(_as_object_list(payload.get("queries"), "queries")):
        context = f"queries[{index}]"
        record = _as_object_map(item, context)
        query_id = _required_string(record, "id", context)
        if query_id in seen_query_ids:
            raise ValueError(f"duplicate query id: {query_id}")
        seen_query_ids.add(query_id)
        relevant = _string_tuple(record, "relevant", context)
        unknown = set(relevant) - seen_document_ids
        if unknown:
            raise ValueError(f"{context}.relevant contains unknown ids: {sorted(unknown)}")
        queries.append(
            QueryCase(
                query_id=query_id,
                dimension=_required_string(record, "dimension", context),
                text=_required_string(record, "text", context),
                relevant=relevant,
            )
        )

    return Corpus(
        description=_required_string(payload, "description", "fixture"),
        license=_required_string(payload, "license", "fixture"),
        provenance=_required_string(payload, "provenance", "fixture"),
        documents=tuple(documents),
        queries=tuple(queries),
    )


def _is_han(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _encode_token(prefix: str, value: str) -> str:
    return prefix + value.encode("utf-8").hex()


def application_tokens(text: str) -> tuple[str, ...]:
    """Return syntax-safe candidate tokens, not a production language tokenizer."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if _is_han(character):
            end = index + 1
            while end < len(normalized) and _is_han(normalized[end]):
                end += 1
            run = normalized[index:end]
            for width in range(1, min(3, len(run)) + 1):
                for offset in range(0, len(run) - width + 1):
                    gram = run[offset : offset + width]
                    tokens.append(_encode_token(f"h{width}", gram))
            index = end
            continue

        if character.isalnum():
            end = index + 1
            while (
                end < len(normalized) and normalized[end].isalnum() and not _is_han(normalized[end])
            ):
                end += 1
            tokens.append(_encode_token("w", normalized[index:end]))
            index = end
            continue

        index += 1

    return tuple(tokens)


def compile_literal_query(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        raise ValueError("query must contain non-whitespace text")
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


def compile_application_query(text: str) -> str:
    tokens = tuple(dict.fromkeys(application_tokens(text)))
    if not tokens:
        raise ValueError("query must contain an alphanumeric or Han token")
    return " AND ".join(f'"{token}"' for token in tokens)


def compile_query(candidate: Candidate, text: str) -> str:
    if candidate.application_tokenized:
        return compile_application_query(text)
    return compile_literal_query(text)


def _create_database(candidate: Candidate) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            tags TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE search_index USING fts5(
            document_id UNINDEXED,
            title,
            summary,
            tags,
            body,
            tokenize='{candidate.sqlite_tokenizer}'
        )
        """
    )
    return connection


def _indexed_text(candidate: Candidate, text: str) -> str:
    if not candidate.application_tokenized:
        return text
    return " ".join(application_tokens(text))


def _replace_current_document(
    connection: sqlite3.Connection,
    candidate: Candidate,
    document: Document,
) -> None:
    tag_text = " ".join(document.tags)
    connection.execute("DELETE FROM search_index WHERE document_id = ?", (document.document_id,))
    connection.execute(
        """
        INSERT OR REPLACE INTO documents(document_id, title, summary, tags, body)
        VALUES (?, ?, ?, ?, ?)
        """,
        (document.document_id, document.title, document.summary, tag_text, document.body),
    )
    connection.execute(
        """
        INSERT INTO search_index(document_id, title, summary, tags, body)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            document.document_id,
            _indexed_text(candidate, document.title),
            _indexed_text(candidate, document.summary),
            _indexed_text(candidate, tag_text),
            _indexed_text(candidate, document.body),
        ),
    )


def _search(
    connection: sqlite3.Connection,
    candidate: Candidate,
    text: str,
    *,
    limit: int = 10,
) -> tuple[SearchHit, ...]:
    compiled = compile_query(candidate, text)
    snippet_expression = (
        "NULL"
        if candidate.application_tokenized
        else "snippet(search_index, 4, '[[', ']]', ' ... ', 12)"
    )
    rows = connection.execute(
        f"""
        SELECT
            document_id,
            bm25(search_index, 0.0, 10.0, 4.0, 6.0, 1.0) AS rank,
            {snippet_expression} AS native_snippet
        FROM search_index
        WHERE search_index MATCH ?
        ORDER BY rank ASC, document_id ASC
        LIMIT ?
        """,
        (compiled, limit),
    ).fetchall()
    return tuple(
        SearchHit(
            document_id=cast(str, row[0]),
            score=float(cast(float, row[1])),
            native_snippet=cast(str | None, row[2]),
        )
        for row in rows
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_candidate(candidate: Candidate, corpus: Corpus) -> dict[str, object]:
    connection = _create_database(candidate)
    try:
        with connection:
            for document in corpus.documents:
                _replace_current_document(connection, candidate, document)

        query_evaluations: list[QueryEvaluation] = []
        recalls_by_dimension: defaultdict[str, list[float]] = defaultdict(list)
        for query in corpus.queries:
            first_hits = _search(connection, candidate, query.text)
            second_hits = _search(connection, candidate, query.text)
            hit_ids = {hit.document_id for hit in first_hits}
            relevant_ids = set(query.relevant)
            recall = len(hit_ids & relevant_ids) / len(relevant_ids)
            precision = len(hit_ids & relevant_ids) / len(hit_ids) if hit_ids else 0.0
            recalls_by_dimension[query.dimension].append(recall)
            query_evaluations.append(
                QueryEvaluation(
                    query_id=query.query_id,
                    dimension=query.dimension,
                    text=query.text,
                    compiled_query=compile_query(candidate, query.text),
                    relevant=query.relevant,
                    hits=first_hits,
                    recall=recall,
                    precision=precision,
                    deterministic=first_hits == second_hits,
                )
            )

        probe_id = "current-only-probe"
        legacy_probe = Document(
            document_id=probe_id,
            title="Current revision probe",
            summary="Synthetic current-only update check.",
            tags=("probe",),
            body="The legacy marker exists only in the previous synthetic revision.",
        )
        current_probe = Document(
            document_id=probe_id,
            title="Current revision probe",
            summary="Synthetic current-only update check.",
            tags=("probe",),
            body="The current marker replaces the previous synthetic revision.",
        )
        with connection:
            _replace_current_document(connection, candidate, legacy_probe)
        legacy_before = {hit.document_id for hit in _search(connection, candidate, "legacy marker")}
        with connection:
            _replace_current_document(connection, candidate, current_probe)
        legacy_after = {hit.document_id for hit in _search(connection, candidate, "legacy marker")}
        current_after = {
            hit.document_id for hit in _search(connection, candidate, "current marker")
        }

        all_recalls = [evaluation.recall for evaluation in query_evaluations]
        return {
            "name": candidate.name,
            "description": candidate.description,
            "sqlite_tokenizer": candidate.sqlite_tokenizer,
            "application_tokenized": candidate.application_tokenized,
            "query_results": [evaluation.as_dict() for evaluation in query_evaluations],
            "overall_mean_recall": _mean(all_recalls),
            "recall_by_dimension": {
                dimension: _mean(values)
                for dimension, values in sorted(recalls_by_dimension.items())
            },
            "deterministic_ranking": all(
                evaluation.deterministic for evaluation in query_evaluations
            ),
            "current_only_update": {
                "legacy_found_before_update": probe_id in legacy_before,
                "legacy_absent_after_update": probe_id not in legacy_after,
                "current_found_after_update": probe_id in current_after,
                "passed": (
                    probe_id in legacy_before
                    and probe_id not in legacy_after
                    and probe_id in current_after
                ),
            },
            "native_snippet_supported": not candidate.application_tokenized,
            "limitations": list(candidate.limitations),
        }
    finally:
        connection.close()


def evaluate_all(corpus: Corpus) -> dict[str, object]:
    return {
        "schema_version": 1,
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_database_version": unicodedata.unidata_version,
        "fixture": {
            "description": corpus.description,
            "license": corpus.license,
            "provenance": corpus.provenance,
            "document_count": len(corpus.documents),
            "query_count": len(corpus.queries),
        },
        "candidates": [evaluate_candidate(candidate, corpus) for candidate in CANDIDATES],
        "scope": (
            "Evaluation only. This output does not select a production tokenizer, ranking "
            "function, query grammar, or snippet contract."
        ),
    }


def _candidate_rows(report: dict[str, object]) -> list[dict[str, object]]:
    candidates = report.get("candidates")
    return [
        _as_object_map(candidate, "report.candidate")
        for candidate in _as_object_list(candidates, "report.candidates")
    ]


def render_markdown(report: dict[str, object]) -> str:
    fixture = _as_object_map(report.get("fixture"), "report.fixture")
    lines = [
        "# SQLite FTS5 Chinese and mixed-language evaluation",
        "",
        f"Python version: `{report['python_version']}`  ",
        f"SQLite version: `{report['sqlite_version']}`  ",
        f"Unicode database version: `{report['unicode_database_version']}`  ",
        f"Fixture: {fixture['document_count']} synthetic documents, "
        f"{fixture['query_count']} queries (`{fixture['license']}`)",
        f"Provenance: {fixture['provenance']}",
        "",
        (
            "| Candidate | Mean recall | Deterministic ranking | Current-only update | "
            "Native snippet |"
        ),
        "| --- | ---: | --- | --- | --- |",
    ]
    for candidate in _candidate_rows(report):
        current_only = _as_object_map(
            candidate.get("current_only_update"), "candidate.current_only_update"
        )
        lines.append(
            "| "
            f"`{candidate['name']}` | {float(cast(float, candidate['overall_mean_recall'])):.3f} | "
            f"{candidate['deterministic_ranking']} | {current_only['passed']} | "
            f"{candidate['native_snippet_supported']} |"
        )

    lines.extend(["", "## Recall by dimension", ""])
    dimensions: set[str] = set()
    for candidate in _candidate_rows(report):
        recalls = _as_object_map(candidate.get("recall_by_dimension"), "recall_by_dimension")
        dimensions.update(recalls)
    header_candidates = _candidate_rows(report)
    lines.append(
        "| Dimension | " + " | ".join(f"`{row['name']}`" for row in header_candidates) + " |"
    )
    lines.append("| --- | " + " | ".join("---:" for _ in header_candidates) + " |")
    for dimension in sorted(dimensions):
        values: list[str] = []
        for candidate in header_candidates:
            recalls = _as_object_map(candidate.get("recall_by_dimension"), "recall_by_dimension")
            values.append(f"{float(cast(float, recalls[dimension])):.3f}")
        lines.append(f"| `{dimension}` | " + " | ".join(values) + " |")

    lines.extend(["", "## Candidate limitations", ""])
    for candidate in _candidate_rows(report):
        lines.append(f"### `{candidate['name']}`")
        lines.append("")
        lines.append(str(candidate["description"]))
        lines.append("")
        for limitation in _as_object_list(candidate.get("limitations"), "limitations"):
            lines.append(f"- {limitation}")
        lines.append("")

    lines.extend(
        [
            "## Boundary",
            "",
            str(report["scope"]),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate SQLite FTS5 candidates on a synthetic multilingual corpus."
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()

    corpus = load_corpus(Path(cast(str, args.fixture)))
    report = evaluate_all(corpus)
    if cast(str, args.format) == "markdown":
        print(render_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
