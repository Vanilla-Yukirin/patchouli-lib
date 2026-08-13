from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import tempfile
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Literal, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "retrieval" / "fts_corpus.json"
BENCHMARK_COPIES_PER_DOCUMENT = 64
BUILD_REPETITIONS = 3
UPDATE_ITERATIONS = 24
QUERY_REPETITIONS = 8
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_QUERY_CODEPOINTS = 2048
MAX_QUERY_UTF8_BYTES = 4096
MAX_QUERY_TOKENS = 64
MAX_SEARCH_CANDIDATES = 256
MAX_SEARCHABLE_PAGES_PER_SECTION = 1024
MAX_DERIVED_TOKENS_PER_DOCUMENT = 65_536
HAN_RANGE_VERSION = "Unicode 15.1"
HAN_RANGES = (
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2B73F),  # Extension C
    (0x2B740, 0x2B81F),  # Extension D
    (0x2B820, 0x2CEAF),  # Extension E
    (0x2CEB0, 0x2EBEF),  # Extension F
    (0x2EBF0, 0x2EE5F),  # Extension I
    (0x2F800, 0x2FA1F),  # CJK Compatibility Ideographs Supplement
    (0x30000, 0x3134F),  # Extension G
    (0x31350, 0x323AF),  # Extension H
)

CandidateName = Literal["unicode61", "trigram", "application_cjk_ngrams"]


@dataclass(frozen=True)
class Document:
    document_id: str
    section_id: str
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


@dataclass(frozen=True)
class LatencySummary:
    samples: int
    median_ms: float
    p95_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
        }


class SearchResourceLimitError(ValueError):
    pass


class SearchIndexResourceLimitError(ValueError):
    pass


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
            "encoded word tokens, and non-whitespace punctuation emits encoded literal tokens. "
            "Each token receives a hashed Section prefix before storage in a unicode61 FTS "
            "table."
        ),
        limitations=(
            "The index expands each Han run and therefore costs more storage and indexing work.",
            "AND over overlapping grams approximates substring matching but is not linguistic "
            "segmentation and can produce false positives when grams occur in different places.",
            "Encoded punctuation prevents FTS grammar expansion but does not promise phrase, "
            "adjacency, or punctuation-order semantics.",
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
                section_id=_required_string(record, "section_id", context),
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
    return any(start <= codepoint <= end for start, end in HAN_RANGES)


def _encode_token(prefix: str, value: str) -> str:
    return prefix + value.encode("utf-8").hex()


def application_tokens(
    text: str,
    *,
    max_tokens: int | None = None,
) -> tuple[str, ...]:
    """Return syntax-safe candidate tokens, not a production language tokenizer."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    seen: set[str] = set()

    def append_unique(token: str) -> None:
        if token not in seen:
            if max_tokens is not None and len(tokens) >= max_tokens:
                raise SearchIndexResourceLimitError("content exceeds the alpha derived-token limit")
            seen.add(token)
            tokens.append(token)

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
                    append_unique(_encode_token(f"h{width}", gram))
            index = end
            continue

        if character.isalnum():
            end = index + 1
            while (
                end < len(normalized) and normalized[end].isalnum() and not _is_han(normalized[end])
            ):
                end += 1
            append_unique(_encode_token("w", normalized[index:end]))
            index = end
            continue

        if not character.isspace():
            append_unique(_encode_token("p", character))

        index += 1

    return tuple(tokens)


def compile_literal_query(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        raise ValueError("query must contain non-whitespace text")
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


def _section_token_prefix(section_id: str) -> str:
    digest = hashlib.sha256(section_id.encode("utf-8")).hexdigest()
    return f"s{digest}x"


def _scope_token(section_id: str, token: str) -> str:
    return _section_token_prefix(section_id) + token


def _validated_query_tokens(text: str) -> tuple[str, ...]:
    if len(text) > MAX_QUERY_CODEPOINTS:
        raise SearchResourceLimitError("query exceeds the alpha codepoint limit")
    if len(text.encode()) > MAX_QUERY_UTF8_BYTES:
        raise SearchResourceLimitError("query exceeds the alpha UTF-8 byte limit")
    tokens = application_tokens(text)
    if not tokens:
        raise ValueError("query must contain an alphanumeric or Han token")
    if len(tokens) > MAX_QUERY_TOKENS:
        raise SearchResourceLimitError("query exceeds the alpha token limit")
    return tokens


def _compile_application_tokens(tokens: tuple[str, ...], section_id: str) -> str:
    return " AND ".join(f'"{_scope_token(section_id, token)}"' for token in tokens)


def compile_application_query(text: str, section_id: str = "section-primary") -> str:
    return _compile_application_tokens(_validated_query_tokens(text), section_id)


def compile_query(
    candidate: Candidate,
    text: str,
    section_id: str = "section-primary",
) -> str:
    if candidate.application_tokenized:
        return compile_application_query(text, section_id)
    _validated_query_tokens(text)
    return compile_literal_query(text)


def _create_database(
    candidate: Candidate,
    database_path: str | Path = ":memory:",
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            section_id TEXT NOT NULL,
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
            section_id UNINDEXED,
            title,
            summary,
            tags,
            body,
            tokenize='{candidate.sqlite_tokenizer}'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE search_rank_tokens (
            section_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            field TEXT NOT NULL CHECK (field IN ('title', 'summary', 'tags', 'body')),
            token TEXT NOT NULL,
            PRIMARY KEY (section_id, document_id, field, token)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX ix_search_rank_tokens_lookup
        ON search_rank_tokens(section_id, token, document_id, field)
        """
    )
    connection.execute(
        """
        CREATE INDEX ix_documents_section_id
        ON documents(section_id, document_id)
        """
    )
    return connection


def _indexed_text(
    candidate: Candidate,
    section_id: str,
    text: str,
    tokens: tuple[str, ...],
) -> str:
    if not candidate.application_tokenized:
        return text
    return " ".join(_scope_token(section_id, token) for token in tokens)


def _bounded_section_document_count(
    connection: sqlite3.Connection,
    section_id: str,
) -> int:
    return cast(
        int,
        connection.execute(
            """
            SELECT count(*)
            FROM (
                SELECT 1
                FROM documents
                WHERE section_id = ?
                LIMIT ?
            ) AS bounded_section_documents
            """,
            (section_id, MAX_SEARCHABLE_PAGES_PER_SECTION + 1),
        ).fetchone()[0],
    )


def _replace_current_document(
    connection: sqlite3.Connection,
    candidate: Candidate,
    document: Document,
) -> None:
    tag_text = " ".join(document.tags)
    existing_row = connection.execute(
        "SELECT section_id FROM documents WHERE document_id = ?",
        (document.document_id,),
    ).fetchone()
    existing_section_id = cast(str | None, existing_row[0] if existing_row else None)
    if (
        existing_section_id != document.section_id
        and _bounded_section_document_count(connection, document.section_id)
        >= MAX_SEARCHABLE_PAGES_PER_SECTION
    ):
        raise SearchIndexResourceLimitError("section exceeds the alpha searchable-page limit")
    if len(document.body.encode()) > MAX_CONTENT_BYTES:
        raise SearchIndexResourceLimitError("content exceeds the alpha byte limit")
    derived_token_count = 0
    rank_fields: list[tuple[str, str, tuple[str, ...]]] = []
    for field, text in (
        ("title", document.title),
        ("summary", document.summary),
        ("tags", tag_text),
        ("body", document.body),
    ):
        remaining = MAX_DERIVED_TOKENS_PER_DOCUMENT - derived_token_count
        tokens = application_tokens(text, max_tokens=remaining)
        derived_token_count += len(tokens)
        rank_fields.append((field, text, tokens))
    connection.execute("DELETE FROM search_index WHERE document_id = ?", (document.document_id,))
    connection.execute(
        "DELETE FROM search_rank_tokens WHERE document_id = ?",
        (document.document_id,),
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO documents(document_id, section_id, title, summary, tags, body)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document.document_id,
            document.section_id,
            document.title,
            document.summary,
            tag_text,
            document.body,
        ),
    )
    connection.execute(
        """
        INSERT INTO search_index(document_id, section_id, title, summary, tags, body)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            document.document_id,
            document.section_id,
            _indexed_text(candidate, document.section_id, document.title, rank_fields[0][2]),
            _indexed_text(candidate, document.section_id, document.summary, rank_fields[1][2]),
            _indexed_text(candidate, document.section_id, tag_text, rank_fields[2][2]),
            _indexed_text(candidate, document.section_id, document.body, rank_fields[3][2]),
        ),
    )
    connection.executemany(
        """
        INSERT INTO search_rank_tokens(section_id, document_id, field, token)
        VALUES (?, ?, ?, ?)
        """,
        (
            (document.section_id, document.document_id, field, token)
            for field, _, tokens in rank_fields
            for token in tokens
        ),
    )


def _search(
    connection: sqlite3.Connection,
    candidate: Candidate,
    text: str,
    *,
    section_id: str = "section-primary",
    limit: int = 10,
) -> tuple[SearchHit, ...]:
    query_tokens = _validated_query_tokens(text)
    if _bounded_section_document_count(connection, section_id) > MAX_SEARCHABLE_PAGES_PER_SECTION:
        raise SearchResourceLimitError("section exceeds the alpha searchable-page limit")
    compiled = (
        _compile_application_tokens(query_tokens, section_id)
        if candidate.application_tokenized
        else compile_literal_query(text)
    )
    candidate_rows = connection.execute(
        """
        SELECT search_index.document_id
        FROM search_index
        WHERE search_index MATCH ? AND search_index.section_id = ?
        LIMIT ?
        """,
        (compiled, section_id, MAX_SEARCH_CANDIDATES + 1),
    ).fetchall()
    candidate_ids = tuple(cast(str, row[0]) for row in candidate_rows)
    if len(candidate_ids) > MAX_SEARCH_CANDIDATES:
        raise SearchResourceLimitError("query exceeds the alpha candidate limit")
    if not candidate_ids:
        return ()

    candidate_values = ", ".join("(?)" for _ in candidate_ids)
    query_token_values = ", ".join("(?)" for _ in query_tokens)
    rank_rows = connection.execute(
        f"""
        WITH
            candidates(document_id) AS (VALUES {candidate_values}),
            query_tokens(token) AS (VALUES {query_token_values})
        SELECT
            candidates.document_id,
            SUM(
                CASE search_rank_tokens.field
                    WHEN 'title' THEN 10.0
                    WHEN 'tags' THEN 6.0
                    WHEN 'summary' THEN 4.0
                    ELSE 1.0
                END
            ) / ? AS rank
        FROM candidates
        JOIN search_rank_tokens
          ON search_rank_tokens.section_id = ?
         AND search_rank_tokens.document_id = candidates.document_id
        JOIN query_tokens ON query_tokens.token = search_rank_tokens.token
        GROUP BY candidates.document_id
        ORDER BY rank DESC, candidates.document_id ASC
        LIMIT ?
        """,
        (*candidate_ids, *query_tokens, len(query_tokens), section_id, limit),
    ).fetchall()

    hits: list[SearchHit] = []
    for row in rank_rows:
        document_id = cast(str, row[0])
        native_snippet: str | None = None
        if not candidate.application_tokenized:
            snippet_row = connection.execute(
                """
                SELECT snippet(search_index, 5, '[[', ']]', ' ... ', 12)
                FROM search_index
                WHERE search_index MATCH ?
                  AND search_index.section_id = ?
                  AND search_index.document_id = ?
                """,
                (compiled, section_id, document_id),
            ).fetchone()
            native_snippet = cast(str, snippet_row[0])
        hits.append(
            SearchHit(
                document_id=document_id,
                score=float(cast(float, row[1])),
                native_snippet=native_snippet,
            )
        )
    return tuple(hits)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _latency_summary(samples_ms: list[float]) -> LatencySummary:
    ordered = sorted(samples_ms)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return LatencySummary(
        samples=len(ordered),
        median_ms=round(median(ordered), 6),
        p95_ms=round(ordered[p95_index], 6),
    )


def _database_bytes(connection: sqlite3.Connection) -> int:
    page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
    page_count = cast(int, connection.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = cast(int, connection.execute("PRAGMA freelist_count").fetchone()[0])
    return page_size * (page_count - freelist_count)


def _benchmark_documents(corpus: Corpus) -> tuple[Document, ...]:
    return tuple(
        Document(
            document_id=f"{document.document_id}-{copy_index:03d}",
            section_id=document.section_id,
            title=document.title,
            summary=document.summary,
            tags=document.tags,
            body=document.body,
        )
        for copy_index in range(BENCHMARK_COPIES_PER_DOCUMENT)
        for document in corpus.documents
    )


def _insert_documents_without_index(
    connection: sqlite3.Connection,
    documents: tuple[Document, ...],
) -> None:
    connection.execute(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            section_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            tags TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO documents(document_id, section_id, title, summary, tags, body)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            (
                document.document_id,
                document.section_id,
                document.title,
                document.summary,
                " ".join(document.tags),
                document.body,
            )
            for document in documents
        ),
    )


def _measure_baseline_database_bytes(documents: tuple[Document, ...]) -> int:
    with tempfile.TemporaryDirectory(prefix="patchouli-fts-baseline-") as temporary_directory:
        database_path = Path(temporary_directory) / "baseline.sqlite3"
        connection = sqlite3.connect(database_path)
        try:
            with connection:
                _insert_documents_without_index(connection, documents)
            connection.execute("VACUUM")
            return _database_bytes(connection)
        finally:
            connection.close()


def _build_candidate_database(
    candidate: Candidate,
    documents: tuple[Document, ...],
    database_path: Path,
) -> tuple[sqlite3.Connection, float]:
    start_ns = time.perf_counter_ns()
    connection = _create_database(candidate, database_path)
    with connection:
        for document in documents:
            _replace_current_document(connection, candidate, document)
    elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
    return connection, elapsed_ms


def _benchmark_candidate(
    candidate: Candidate,
    corpus: Corpus,
    documents: tuple[Document, ...],
    baseline_database_bytes: int,
) -> dict[str, object]:
    build_samples_ms: list[float] = []
    with tempfile.TemporaryDirectory(prefix=f"patchouli-fts-{candidate.name}-") as directory:
        temporary_directory = Path(directory)
        benchmark_connection: sqlite3.Connection | None = None
        for repetition in range(BUILD_REPETITIONS):
            database_path = temporary_directory / f"candidate-{repetition}.sqlite3"
            connection, elapsed_ms = _build_candidate_database(
                candidate,
                documents,
                database_path,
            )
            build_samples_ms.append(elapsed_ms)
            if benchmark_connection is None:
                benchmark_connection = connection
            else:
                connection.close()

        assert benchmark_connection is not None
        try:
            benchmark_connection.execute("VACUUM")
            total_database_bytes = _database_bytes(benchmark_connection)

            for query in corpus.queries:
                _search(benchmark_connection, candidate, query.text)
            query_samples_ms: list[float] = []
            for _ in range(QUERY_REPETITIONS):
                for query in corpus.queries:
                    start_ns = time.perf_counter_ns()
                    _search(benchmark_connection, candidate, query.text)
                    query_samples_ms.append((time.perf_counter_ns() - start_ns) / 1_000_000)

            source_document = documents[0]
            update_samples_ms: list[float] = []
            for iteration in range(UPDATE_ITERATIONS):
                updated_document = Document(
                    document_id=source_document.document_id,
                    section_id=source_document.section_id,
                    title=source_document.title,
                    summary=source_document.summary,
                    tags=source_document.tags,
                    body=f"{source_document.body} Synthetic update marker {iteration % 2}.",
                )
                start_ns = time.perf_counter_ns()
                with benchmark_connection:
                    _replace_current_document(benchmark_connection, candidate, updated_document)
                update_samples_ms.append((time.perf_counter_ns() - start_ns) / 1_000_000)

            index_overhead_bytes = max(0, total_database_bytes - baseline_database_bytes)
            return {
                "database_bytes": total_database_bytes,
                "index_overhead_bytes": index_overhead_bytes,
                "index_overhead_bytes_per_document": round(
                    index_overhead_bytes / len(documents),
                    3,
                ),
                "build_latency": _latency_summary(build_samples_ms).as_dict(),
                "update_latency": _latency_summary(update_samples_ms).as_dict(),
                "query_latency": _latency_summary(query_samples_ms).as_dict(),
            }
        finally:
            benchmark_connection.close()


def _high_entropy_han_body() -> str:
    required_characters = MAX_DERIVED_TOKENS_PER_DOCUMENT + 1
    characters: list[str] = []
    for start, end in HAN_RANGES:
        if start >= 0xF900 and start < 0x20000:
            continue
        for codepoint in range(start, end + 1):
            characters.append(chr(codepoint))
            if len(characters) == required_characters:
                seed = "".join(characters)
                seed_bytes = len(seed.encode())
                repetitions, remainder = divmod(MAX_CONTENT_BYTES, seed_bytes)
                return seed * repetitions + (" " * remainder)
    raise AssertionError("versioned Han ranges do not contain enough synthetic characters")


def _evaluate_application_resource_guardrails(candidate: Candidate) -> dict[str, object]:
    connection = _create_database(candidate)
    try:
        max_body = "界 " * (MAX_CONTENT_BYTES // len("界 ".encode()))
        max_body_document = Document(
            document_id="max-body-authorized",
            section_id="section-max-body",
            title="Maximum body probe",
            summary="Synthetic maximum body query evidence.",
            tags=("boundary",),
            body=max_body,
        )
        with connection:
            _replace_current_document(connection, candidate, max_body_document)
        start_ns = time.perf_counter_ns()
        max_body_hits = _search(
            connection,
            candidate,
            "界",
            section_id="section-max-body",
        )
        max_body_query_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

        high_entropy_body = _high_entropy_han_body()
        high_entropy_document = Document(
            document_id="max-derived-token-overflow",
            section_id="section-max-body",
            title="Derived token overflow probe",
            summary="Synthetic high-entropy indexing evidence.",
            tags=("boundary",),
            body=high_entropy_body,
        )
        high_entropy_failed_closed = False
        try:
            with connection:
                _replace_current_document(connection, candidate, high_entropy_document)
        except SearchIndexResourceLimitError:
            high_entropy_failed_closed = True
        high_entropy_rows = cast(
            int,
            connection.execute(
                "SELECT count(*) FROM documents WHERE document_id = ?",
                (high_entropy_document.document_id,),
            ).fetchone()[0],
        )

        authorized_broad_documents = tuple(
            Document(
                document_id=f"broad-authorized-{index:03d}",
                section_id="section-broad-authorized",
                title="Broad boundary",
                summary="Synthetic candidate-ceiling evidence.",
                tags=("boundary",),
                body=f"Broad boundary marker {index}.",
            )
            for index in range(MAX_SEARCH_CANDIDATES)
        )
        unauthorized_broad_documents = tuple(
            Document(
                document_id=f"broad-unauthorized-{index:03d}",
                section_id="section-broad-unauthorized",
                title="Broad boundary",
                summary="Synthetic other-Section ceiling evidence.",
                tags=("boundary",),
                body=(
                    f"Broad boundary marker {index}. "
                    + (
                        "leftpartition"
                        if index < MAX_SEARCHABLE_PAGES_PER_SECTION // 2
                        else "rightpartition"
                    )
                ),
            )
            for index in range(MAX_SEARCHABLE_PAGES_PER_SECTION)
        )
        with connection:
            for document in (*authorized_broad_documents, *unauthorized_broad_documents):
                _replace_current_document(connection, candidate, document)

        start_ns = time.perf_counter_ns()
        broad_hits = _search(
            connection,
            candidate,
            "broad boundary",
            section_id="section-broad-authorized",
            limit=MAX_SEARCH_CANDIDATES,
        )
        broad_query_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
        unauthorized_limit_failed_closed = False
        compiled_broad_query = compile_application_query(
            "broad boundary",
            "section-broad-unauthorized",
        )
        recall_query_plan = [
            cast(str, row[3])
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT search_index.document_id
                FROM search_index
                WHERE search_index MATCH ? AND search_index.section_id = ?
                LIMIT ?
                """,
                (
                    compiled_broad_query,
                    "section-broad-unauthorized",
                    MAX_SEARCH_CANDIDATES + 1,
                ),
            ).fetchall()
        ]
        recall_vm_opcodes = [
            cast(str, row[1])
            for row in connection.execute(
                """
                EXPLAIN
                SELECT search_index.document_id
                FROM search_index
                WHERE search_index MATCH ? AND search_index.section_id = ?
                LIMIT ?
                """,
                (
                    compiled_broad_query,
                    "section-broad-unauthorized",
                    MAX_SEARCH_CANDIDATES + 1,
                ),
            ).fetchall()
        ]
        limit_guard_index = recall_vm_opcodes.index("DecrJumpZero")
        next_match_index = recall_vm_opcodes.index("VNext")
        try:
            _search(
                connection,
                candidate,
                "broad boundary",
                section_id="section-broad-unauthorized",
                limit=MAX_SEARCH_CANDIDATES,
            )
        except SearchResourceLimitError:
            unauthorized_limit_failed_closed = True

        late_intersection_hits = _search(
            connection,
            candidate,
            "leftpartition rightpartition",
            section_id="section-broad-unauthorized",
        )
        section_write_overflow_failed_closed = False
        overflow_document = Document(
            document_id="same-section-page-overflow",
            section_id="section-broad-unauthorized",
            title="Synthetic over-limit Page",
            summary="Adversarial Section work-bound evidence.",
            tags=("boundary",),
            body="leftpartition rightpartition",
        )
        try:
            with connection:
                _replace_current_document(connection, candidate, overflow_document)
        except SearchIndexResourceLimitError:
            section_write_overflow_failed_closed = True
        durable_pages_after_failed_write = _bounded_section_document_count(
            connection,
            "section-broad-unauthorized",
        )

        # Simulate a pre-existing incompatible projection. Search must reject it
        # before MATCH instead of leaking a partial result from the first 1,024 rows.
        connection.execute(
            """
            INSERT INTO documents(document_id, section_id, title, summary, tags, body)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                overflow_document.document_id,
                overflow_document.section_id,
                overflow_document.title,
                overflow_document.summary,
                " ".join(overflow_document.tags),
                overflow_document.body,
            ),
        )
        section_query_overflow_failed_closed = False
        partial_overflow_hits: tuple[SearchHit, ...] = ()
        try:
            partial_overflow_hits = _search(
                connection,
                candidate,
                "leftpartition rightpartition",
                section_id="section-broad-unauthorized",
            )
        except SearchResourceLimitError:
            section_query_overflow_failed_closed = True

        query_token_overflow_failed_closed = False
        try:
            _search(
                connection,
                candidate,
                " ".join(f"term{index}" for index in range(MAX_QUERY_TOKENS + 1)),
                section_id="section-broad-authorized",
            )
        except SearchResourceLimitError:
            query_token_overflow_failed_closed = True

        query_codepoint_overflow_failed_closed = False
        try:
            _search(
                connection,
                candidate,
                "x" * (MAX_QUERY_CODEPOINTS + 1),
                section_id="section-broad-authorized",
            )
        except SearchResourceLimitError:
            query_codepoint_overflow_failed_closed = True

        query_byte_overflow_failed_closed = False
        byte_overflow_query = "界" * ((MAX_QUERY_UTF8_BYTES // 3) + 1)
        try:
            _search(
                connection,
                candidate,
                byte_overflow_query,
                section_id="section-broad-authorized",
            )
        except SearchResourceLimitError:
            query_byte_overflow_failed_closed = True

        authorized_query = compile_application_query("界", "section-max-body")
        unauthorized_query = compile_application_query("界", "section-other")
        return {
            "max_content_bytes": MAX_CONTENT_BYTES,
            "max_derived_tokens_per_document": MAX_DERIVED_TOKENS_PER_DOCUMENT,
            "max_query_codepoints": MAX_QUERY_CODEPOINTS,
            "max_query_utf8_bytes": MAX_QUERY_UTF8_BYTES,
            "max_query_tokens": MAX_QUERY_TOKENS,
            "max_candidates": MAX_SEARCH_CANDIDATES,
            "max_searchable_pages_per_section": MAX_SEARCHABLE_PAGES_PER_SECTION,
            "query_time_body_tokenization": False,
            "max_body_query": {
                "body_bytes": len(max_body.encode("utf-8")),
                "hit_ids": [hit.document_id for hit in max_body_hits],
                "elapsed_ms": round(max_body_query_ms, 6),
                "passed": tuple(hit.document_id for hit in max_body_hits)
                == ("max-body-authorized",),
            },
            "high_entropy_index": {
                "body_bytes": len(high_entropy_body.encode()),
                "failed_closed": high_entropy_failed_closed,
                "durable_document_rows": high_entropy_rows,
                "passed": high_entropy_failed_closed and high_entropy_rows == 0,
            },
            "broad_match_at_limit": {
                "authorized_candidates": len(broad_hits),
                "other_section_candidates": len(unauthorized_broad_documents),
                "elapsed_ms": round(broad_query_ms, 6),
                "passed": len(broad_hits) == MAX_SEARCH_CANDIDATES,
            },
            "recall_query_plan": {
                "details": recall_query_plan,
                "uses_temporary_sort": any(
                    "USE TEMP B-TREE" in detail.upper() for detail in recall_query_plan
                ),
                "vm_opcodes": recall_vm_opcodes,
                "limit_guard_precedes_next_match": (limit_guard_index < next_match_index),
            },
            "candidate_overflow_failed_closed": unauthorized_limit_failed_closed,
            "same_section_late_intersection": {
                "pages_at_limit": len(unauthorized_broad_documents),
                "hit_ids_at_limit": [hit.document_id for hit in late_intersection_hits],
                "write_overflow_failed_closed": section_write_overflow_failed_closed,
                "durable_pages_after_failed_write": durable_pages_after_failed_write,
                "query_overflow_failed_closed": section_query_overflow_failed_closed,
                "partial_hit_ids_on_overflow": [hit.document_id for hit in partial_overflow_hits],
                "passed": (
                    not late_intersection_hits
                    and section_write_overflow_failed_closed
                    and durable_pages_after_failed_write == MAX_SEARCHABLE_PAGES_PER_SECTION
                    and section_query_overflow_failed_closed
                    and not partial_overflow_hits
                ),
            },
            "query_codepoint_overflow_failed_closed": (query_codepoint_overflow_failed_closed),
            "query_byte_overflow_failed_closed": query_byte_overflow_failed_closed,
            "query_token_overflow_failed_closed": query_token_overflow_failed_closed,
            "section_scoped_postings": {
                "authorized_compiled_query": authorized_query,
                "other_section_compiled_query": unauthorized_query,
                "passed": authorized_query != unauthorized_query,
            },
        }
    finally:
        connection.close()


def evaluate_candidate(
    candidate: Candidate,
    corpus: Corpus,
    benchmark_documents: tuple[Document, ...],
    baseline_database_bytes: int,
) -> dict[str, object]:
    connection = _create_database(candidate)
    try:
        with connection:
            for document in corpus.documents:
                _replace_current_document(connection, candidate, document)

        query_evaluations: list[QueryEvaluation] = []
        recalls_by_dimension: defaultdict[str, list[float]] = defaultdict(list)
        precisions_by_dimension: defaultdict[str, list[float]] = defaultdict(list)
        for query in corpus.queries:
            first_hits = _search(connection, candidate, query.text)
            second_hits = _search(connection, candidate, query.text)
            hit_ids = {hit.document_id for hit in first_hits}
            relevant_ids = set(query.relevant)
            recall = len(hit_ids & relevant_ids) / len(relevant_ids)
            precision = len(hit_ids & relevant_ids) / len(hit_ids) if hit_ids else 0.0
            recalls_by_dimension[query.dimension].append(recall)
            precisions_by_dimension[query.dimension].append(precision)
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
            section_id="section-primary",
            title="Current revision probe",
            summary="Synthetic current-only update check.",
            tags=("probe",),
            body="The legacy marker exists only in the previous synthetic revision.",
        )
        current_probe = Document(
            document_id=probe_id,
            section_id="section-primary",
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

        tie_documents = (
            Document(
                document_id="ranking-z",
                section_id="section-primary",
                title="Deterministic tie marker",
                summary="Synthetic ranking probe.",
                tags=("ranking",),
                body="This equal document verifies a stable identifier tie break.",
            ),
            Document(
                document_id="ranking-a",
                section_id="section-primary",
                title="Deterministic tie marker",
                summary="Synthetic ranking probe.",
                tags=("ranking",),
                body="This equal document verifies a stable identifier tie break.",
            ),
        )
        with connection:
            for tie_document in tie_documents:
                _replace_current_document(connection, candidate, tie_document)
        tie_order = tuple(
            hit.document_id
            for hit in _search(connection, candidate, "deterministic tie marker")
            if hit.document_id.startswith("ranking-")
        )
        tie_break_passed = tie_order == ("ranking-a", "ranking-z")

        authorized_documents = (
            Document(
                document_id="section-authorized-title",
                section_id="section-authorized",
                title="Isolation",
                summary="Synthetic authorized title match.",
                tags=("boundary",),
                body="The authorized Section contains this result.",
            ),
            Document(
                document_id="section-authorized-body",
                section_id="section-authorized",
                title="Authorized body result",
                summary="Synthetic authorized body match.",
                tags=("boundary",),
                body="A weaker isolation term occurs only in this body.",
            ),
        )
        with connection:
            for authorized_document in authorized_documents:
                _replace_current_document(connection, candidate, authorized_document)
        authorized_before = _search(
            connection,
            candidate,
            "isolation",
            section_id="section-authorized",
            limit=100,
        )
        unauthorized_documents = tuple(
            Document(
                document_id=f"section-unauthorized-{index:02d}",
                section_id="section-unauthorized",
                title="Section isolation marker",
                summary="Synthetic unauthorized result with repeated isolation terms.",
                tags=("isolation",),
                body=("isolation " * 32) + f"unauthorized synthetic decoy {index}",
            )
            for index in range(12)
        )
        with connection:
            for unauthorized_document in unauthorized_documents:
                _replace_current_document(connection, candidate, unauthorized_document)
        authorized_after = _search(
            connection,
            candidate,
            "isolation",
            section_id="section-authorized",
            limit=100,
        )
        authorized_limited = _search(
            connection,
            candidate,
            "isolation",
            section_id="section-authorized",
            limit=1,
        )
        unauthorized_hits = _search(
            connection,
            candidate,
            "isolation",
            section_id="section-unauthorized",
            limit=1,
        )
        authorized_before_ids = [hit.document_id for hit in authorized_before]
        authorized_after_ids = [hit.document_id for hit in authorized_after]
        authorized_before_scores = [hit.score for hit in authorized_before]
        authorized_after_scores = [hit.score for hit in authorized_after]
        cross_section_isolation = {
            "authorized_rank_order_before": authorized_before_ids,
            "authorized_rank_order_after": authorized_after_ids,
            "authorized_rank_scores_before": authorized_before_scores,
            "authorized_rank_scores_after": authorized_after_scores,
            "authorized_result_count_before": len(authorized_before),
            "authorized_result_count_after": len(authorized_after),
            "authorized_limit_one_hit_ids": [hit.document_id for hit in authorized_limited],
            "unauthorized_probe_hit_ids": [hit.document_id for hit in unauthorized_hits],
            "passed": (
                authorized_before_ids
                == authorized_after_ids
                == ["section-authorized-title", "section-authorized-body"]
                and authorized_before_scores == authorized_after_scores
                and len(authorized_before) == len(authorized_after) == 2
                and tuple(hit.document_id for hit in authorized_limited)
                == ("section-authorized-title",)
                and all(
                    not hit.document_id.startswith("section-unauthorized")
                    for hit in authorized_after
                )
            ),
        }

        all_recalls = [evaluation.recall for evaluation in query_evaluations]
        all_precisions = [evaluation.precision for evaluation in query_evaluations]
        return {
            "name": candidate.name,
            "description": candidate.description,
            "sqlite_tokenizer": candidate.sqlite_tokenizer,
            "application_tokenized": candidate.application_tokenized,
            "query_results": [evaluation.as_dict() for evaluation in query_evaluations],
            "overall_mean_recall": _mean(all_recalls),
            "overall_mean_precision": _mean(all_precisions),
            "recall_by_dimension": {
                dimension: _mean(values)
                for dimension, values in sorted(recalls_by_dimension.items())
            },
            "precision_by_dimension": {
                dimension: _mean(values)
                for dimension, values in sorted(precisions_by_dimension.items())
            },
            "deterministic_ranking": (
                all(evaluation.deterministic for evaluation in query_evaluations)
                and tie_break_passed
            ),
            "ranking_tie_break": {
                "order": list(tie_order),
                "passed": tie_break_passed,
            },
            "cross_section_isolation": cross_section_isolation,
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
            "resource_metrics": _benchmark_candidate(
                candidate,
                corpus,
                benchmark_documents,
                baseline_database_bytes,
            ),
            "resource_guardrails": (
                _evaluate_application_resource_guardrails(candidate)
                if candidate.application_tokenized
                else None
            ),
            "limitations": list(candidate.limitations),
        }
    finally:
        connection.close()


def evaluate_all(corpus: Corpus) -> dict[str, object]:
    benchmark_documents = _benchmark_documents(corpus)
    baseline_database_bytes = _measure_baseline_database_bytes(benchmark_documents)
    return {
        "schema_version": 2,
        "python_version": platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "unicode_database_version": unicodedata.unidata_version,
        "han_range_version": HAN_RANGE_VERSION,
        "fixture": {
            "description": corpus.description,
            "license": corpus.license,
            "provenance": corpus.provenance,
            "document_count": len(corpus.documents),
            "query_count": len(corpus.queries),
        },
        "resource_benchmark": {
            "copies_per_fixture_document": BENCHMARK_COPIES_PER_DOCUMENT,
            "document_count": len(benchmark_documents),
            "baseline_database_bytes": baseline_database_bytes,
            "build_repetitions": BUILD_REPETITIONS,
            "update_iterations": UPDATE_ITERATIONS,
            "query_repetitions_per_fixture_query": QUERY_REPETITIONS,
            "timing_scope": (
                "Indicative wall-clock measurements from one local process; values are not "
                "release thresholds or cross-machine guarantees."
            ),
        },
        "candidates": [
            evaluate_candidate(
                candidate,
                corpus,
                benchmark_documents,
                baseline_database_bytes,
            )
            for candidate in CANDIDATES
        ],
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
    benchmark = _as_object_map(report.get("resource_benchmark"), "report.resource_benchmark")
    lines = [
        "# SQLite FTS5 Chinese and mixed-language evaluation",
        "",
        f"Python version: `{report['python_version']}`  ",
        f"SQLite version: `{report['sqlite_version']}`  ",
        f"Unicode database version: `{report['unicode_database_version']}`  ",
        f"Han range table: `{report['han_range_version']}`  ",
        f"Fixture: {fixture['document_count']} synthetic documents, "
        f"{fixture['query_count']} queries (`{fixture['license']}`)",
        f"Provenance: {fixture['provenance']}",
        f"Resource benchmark: {benchmark['document_count']} replicated synthetic documents; "
        f"{benchmark['timing_scope']}",
        "",
        (
            "| Candidate | Mean recall | Mean precision | Deterministic ranking | "
            "Current-only update | Section isolation | Native snippet |"
        ),
        "| --- | ---: | ---: | --- | --- | --- | --- |",
    ]
    for candidate in _candidate_rows(report):
        current_only = _as_object_map(
            candidate.get("current_only_update"), "candidate.current_only_update"
        )
        section_isolation = _as_object_map(
            candidate.get("cross_section_isolation"),
            "candidate.cross_section_isolation",
        )
        lines.append(
            "| "
            f"`{candidate['name']}` | {float(cast(float, candidate['overall_mean_recall'])):.3f} | "
            f"{float(cast(float, candidate['overall_mean_precision'])):.3f} | "
            f"{candidate['deterministic_ranking']} | {current_only['passed']} | "
            f"{section_isolation['passed']} | "
            f"{candidate['native_snippet_supported']} |"
        )

    lines.extend(["", "## Recall and precision by dimension", ""])
    dimensions: set[str] = set()
    for candidate in _candidate_rows(report):
        recalls = _as_object_map(candidate.get("recall_by_dimension"), "recall_by_dimension")
        dimensions.update(recalls)
    header_candidates = _candidate_rows(report)
    candidate_columns = [
        column
        for row in header_candidates
        for column in (f"`{row['name']}` recall", f"`{row['name']}` precision")
    ]
    lines.append("| Dimension | " + " | ".join(candidate_columns) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in candidate_columns) + " |")
    for dimension in sorted(dimensions):
        values: list[str] = []
        for candidate in header_candidates:
            recalls = _as_object_map(candidate.get("recall_by_dimension"), "recall_by_dimension")
            precisions = _as_object_map(
                candidate.get("precision_by_dimension"),
                "precision_by_dimension",
            )
            values.extend(
                (
                    f"{float(cast(float, recalls[dimension])):.3f}",
                    f"{float(cast(float, precisions[dimension])):.3f}",
                )
            )
        lines.append(f"| `{dimension}` | " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Resource measurements",
            "",
            (
                "| Candidate | Index overhead bytes | Bytes/document | Build median ms | "
                "Update median ms | Query median ms | Query p95 ms |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for candidate in header_candidates:
        metrics = _as_object_map(candidate.get("resource_metrics"), "resource_metrics")
        build_latency = _as_object_map(metrics.get("build_latency"), "build_latency")
        update_latency = _as_object_map(metrics.get("update_latency"), "update_latency")
        query_latency = _as_object_map(metrics.get("query_latency"), "query_latency")
        lines.append(
            f"| `{candidate['name']}` | {metrics['index_overhead_bytes']} | "
            f"{float(cast(float, metrics['index_overhead_bytes_per_document'])):.3f} | "
            f"{float(cast(float, build_latency['median_ms'])):.3f} | "
            f"{float(cast(float, update_latency['median_ms'])):.3f} | "
            f"{float(cast(float, query_latency['median_ms'])):.3f} | "
            f"{float(cast(float, query_latency['p95_ms'])):.3f} |"
        )

    proposed = next(
        candidate
        for candidate in header_candidates
        if candidate["name"] == "application_cjk_ngrams"
    )
    guardrails = _as_object_map(proposed.get("resource_guardrails"), "resource_guardrails")
    max_body_query = _as_object_map(guardrails.get("max_body_query"), "max_body_query")
    high_entropy_index = _as_object_map(
        guardrails.get("high_entropy_index"),
        "high_entropy_index",
    )
    broad_match = _as_object_map(
        guardrails.get("broad_match_at_limit"),
        "broad_match_at_limit",
    )
    scoped_postings = _as_object_map(
        guardrails.get("section_scoped_postings"),
        "section_scoped_postings",
    )
    recall_query_plan = _as_object_map(
        guardrails.get("recall_query_plan"),
        "recall_query_plan",
    )
    late_intersection = _as_object_map(
        guardrails.get("same_section_late_intersection"),
        "same_section_late_intersection",
    )
    lines.extend(
        [
            "",
            "## Proposed-candidate resource guardrails",
            "",
            f"- Maximum content probe: {max_body_query['body_bytes']} bytes, "
            f"passed `{max_body_query['passed']}`.",
            f"- High-entropy derived-token overflow: {high_entropy_index['body_bytes']} bytes, "
            f"failed closed `{high_entropy_index['passed']}`.",
            f"- Broad match: {broad_match['authorized_candidates']} authorized candidates "
            f"with {broad_match['other_section_candidates']} same-term candidates in another "
            f"Section, passed `{broad_match['passed']}`.",
            f"- Candidate overflow failed closed: "
            f"`{guardrails['candidate_overflow_failed_closed']}`.",
            f"- Query-token overflow failed closed: "
            f"`{guardrails['query_token_overflow_failed_closed']}`.",
            f"- Query-codepoint overflow failed closed: "
            f"`{guardrails['query_codepoint_overflow_failed_closed']}`.",
            f"- Query-byte overflow failed closed: "
            f"`{guardrails['query_byte_overflow_failed_closed']}`.",
            f"- Query-time body tokenization: `{guardrails['query_time_body_tokenization']}`.",
            f"- Section-scoped posting terms: `{scoped_postings['passed']}`.",
            f"- Recall query uses a temporary sort: `{recall_query_plan['uses_temporary_sort']}`.",
            f"- Recall LIMIT guard precedes the next-match VM step: "
            f"`{recall_query_plan['limit_guard_precedes_next_match']}`.",
            f"- Same-Section late/disjoint intersection at "
            f"{late_intersection['pages_at_limit']} Pages, plus over-limit fail-closed: "
            f"`{late_intersection['passed']}`.",
        ]
    )

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
