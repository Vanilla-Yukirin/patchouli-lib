"""Transactional, rebuildable candidate index for current Page search.

The authority remains the Page/Revision/file graph. SQL triggers only mark a
Page dirty; this module projects complete current snapshots before an ordinary
application write commits. A dirty or incompatible index is never searchable.
This module does not publish or enable a search API by itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine, select

from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.content.models import Page
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.search.literal_v2 import (
    GRAM_VERSION,
    UNICODE_DATA_VERSION,
    encoded_grams,
    normalize_literal,
)
from patchouli_lib.search.text_classification_v2 import (
    TEXT_CLASSIFICATION_VERSION,
    classify_page_manifest,
)

if TYPE_CHECKING:
    from patchouli_lib.content.file_manifest import FileManifest

INDEX_VERSION = f"v2:{GRAM_VERSION}:{TEXT_CLASSIFICATION_VERSION}:u{UNICODE_DATA_VERSION}"


class SearchIndexUnavailableError(RuntimeError):
    """The derived index cannot presently prove complete current coverage."""


class SearchIndexProjectionError(RuntimeError):
    """An authority snapshot cannot be projected in full and atomically."""


@dataclass(frozen=True, slots=True)
class SearchIndexState:
    generation: int
    dirty_sequence: int


@dataclass(frozen=True, slots=True)
class _Document:
    key: str
    kind: str
    file_name: str | None
    text: str
    source_sha256: bytes


def _meta(connection: Connection) -> tuple[int | None, bool, int, str] | None:
    """Return None on pre-0024 schema without querying a private table name."""

    has_table = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'search_meta'"
    ).scalar_one_or_none()
    if has_table is None:
        return None
    row = connection.exec_driver_sql(
        "SELECT active_generation, ready, dirty_sequence, index_version "
        "FROM search_meta WHERE singleton = 1"
    ).one_or_none()
    if row is None:
        raise SearchIndexUnavailableError("Search index metadata is incomplete.")
    generation, ready, sequence, version = row
    if (
        (generation is not None and (type(generation) is not int or generation < 1))
        or ready not in (0, 1)
        or type(sequence) is not int
        or sequence < 0
        or type(version) is not str
    ):
        raise SearchIndexUnavailableError("Search index metadata is invalid.")
    return generation, bool(ready), sequence, version


def dirty_sequence_at_transaction_start(connection: Connection) -> int | None:
    """Capture the high-water mark before a caller-owned content transaction."""

    metadata = _meta(connection)
    return None if metadata is None else metadata[2]


def require_ready_index(connection: Connection) -> SearchIndexState:
    """Require the active, compatible generation in this transaction.

    A selected-scope dirty check remains the search reader's responsibility;
    this check deliberately does not clear or rebuild dirty Pages.
    """

    metadata = _meta(connection)
    if metadata is None:
        raise SearchIndexUnavailableError("Search index schema is missing.")
    generation, ready, sequence, version = metadata
    if not ready or generation is None or version != INDEX_VERSION:
        raise SearchIndexUnavailableError("Search index is not ready.")
    state = connection.exec_driver_sql(
        "SELECT state, index_version FROM search_generations WHERE generation = ?",
        (generation,),
    ).one_or_none()
    if state is None or state[0] != "ready" or state[1] != INDEX_VERSION:
        raise SearchIndexUnavailableError("Search index generation is not ready.")
    return SearchIndexState(generation, sequence)


def _source_digest(page: PageRecord, manifest: FileManifest) -> bytes:
    metadata = {
        "book_id": page.book_id,
        "library_id": page.library_id,
        "occurred_at": page.occurred_at,
        "page_id": page.page_id,
        "revision_id": page.current_revision_id,
        "revision_number": page.current_revision_number,
        "section_id": page.section_id,
        "snapshot_sha256": manifest.snapshot_sha256.hex(),
        "title": page.title,
    }
    serialized = json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(b"patchouli-search-page-v2\0" + serialized).digest()


def _documents(
    page: PageRecord, manifest: FileManifest, repository: ContentRepository
) -> tuple[_Document, ...]:
    storage_format = repository.get_current_revision_storage_format(page)
    classified = classify_page_manifest(manifest, storage_format=storage_format)
    documents = [
        _Document(
            key="title",
            kind="title",
            file_name=None,
            text=page.title,
            source_sha256=hashlib.sha256(page.title.encode("utf-8", errors="strict")).digest(),
        )
    ]
    for source, entry in zip(manifest.files, classified.files, strict=True):
        if source.name != entry.name:
            raise SearchIndexProjectionError("Page file classification changed its source order.")
        documents.append(
            _Document(
                key=f"file_name:{entry.name}",
                kind="file_name",
                file_name=entry.name,
                text=entry.name,
                source_sha256=hashlib.sha256(entry.name.encode("utf-8")).digest(),
            )
        )
        if entry.text is not None:
            documents.append(
                _Document(
                    key=f"file_text:{entry.name}",
                    kind="file_text",
                    file_name=entry.name,
                    text=entry.text,
                    source_sha256=source.content_sha256,
                )
            )
    return tuple(documents)


def _erase_page(connection: Connection, generation: int, library_id: str, page_uid: bytes) -> None:
    identifiers = (
        connection.exec_driver_sql(
            "SELECT id FROM search_documents "
            "WHERE generation = ? AND library_id = ? AND page_uid = ?",
            (generation, library_id, page_uid),
        )
        .scalars()
        .all()
    )
    for document_id in identifiers:
        connection.exec_driver_sql("DELETE FROM search_terms WHERE rowid = ?", (document_id,))
    connection.exec_driver_sql(
        "DELETE FROM search_documents WHERE generation = ? AND library_id = ? AND page_uid = ?",
        (generation, library_id, page_uid),
    )
    connection.exec_driver_sql(
        "DELETE FROM search_page_state WHERE generation = ? AND library_id = ? AND page_uid = ?",
        (generation, library_id, page_uid),
    )


def _project_page(
    connection: Connection, generation: int, library_id: str, page_uid: bytes
) -> None:
    """Replace one Page's complete active-generation projection in this txn."""

    _erase_page(connection, generation, library_id, page_uid)
    row = (
        connection.execute(
            select(Page.__table__).where(
                Page.library_id == library_id,
                Page.page_uid == page_uid,
                Page.deleted_at.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    page = PageRecord.model_validate(dict(row))
    repository = ContentRepository(connection)
    manifest = repository.get_current_file_manifest(page)
    documents = _documents(page, manifest, repository)
    connection.exec_driver_sql(
        "INSERT INTO search_page_state "
        "(generation, library_id, page_uid, section_id, book_id, page_id, revision_id, "
        "revision_number, occurred_at, snapshot_sha256, source_sha256, document_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            generation,
            page.library_id,
            page.page_uid,
            page.section_id,
            page.book_id,
            page.page_id,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            manifest.snapshot_sha256,
            _source_digest(page, manifest),
            len(documents),
        ),
    )
    for document in documents:
        normalized = normalize_literal(document.text)
        tokens = " ".join(encoded_grams(document.text))
        result = connection.exec_driver_sql(
            "INSERT INTO search_documents "
            "(generation, library_id, page_uid, revision_id, revision_number, "
            "document_key, source_kind, file_name, normalized_text, source_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation,
                page.library_id,
                page.page_uid,
                page.current_revision_id,
                page.current_revision_number,
                document.key,
                document.kind,
                document.file_name,
                normalized,
                document.source_sha256,
            ),
        )
        if result.lastrowid is None:
            raise SearchIndexProjectionError("Search document identity was not allocated.")
        connection.exec_driver_sql(
            "INSERT INTO search_terms (rowid, grams) VALUES (?, ?)",
            (result.lastrowid, tokens),
        )


def flush_dirty_since(connection: Connection, start_sequence: int | None) -> int:
    """Reproject only Pages dirtied by this transaction before its commit.

    Earlier dirty rows (e.g. a bypassed writer) remain dirty and block search.
    An ordinary authentication or request-log transaction with no new dirty
    sequence does no projection work.
    """

    if start_sequence is None:
        return 0
    metadata = _meta(connection)
    if metadata is None:
        return 0
    generation, ready, sequence, version = metadata
    if sequence <= start_sequence or not ready or generation is None or version != INDEX_VERSION:
        return 0
    require_ready_index(connection)
    dirty = connection.exec_driver_sql(
        "SELECT library_id, page_uid, seq FROM search_dirty_pages "
        "WHERE seq > ? ORDER BY seq, library_id, page_uid",
        (start_sequence,),
    ).all()
    for library_id, page_uid, row_sequence in dirty:
        _project_page(connection, generation, library_id, page_uid)
        deleted = connection.exec_driver_sql(
            "DELETE FROM search_dirty_pages WHERE library_id = ? AND page_uid = ? AND seq = ?",
            (library_id, page_uid, row_sequence),
        )
        if deleted.rowcount != 1:
            raise SearchIndexProjectionError("Dirty Page sequence changed during projection.")
    return len(dirty)


def rebuild_search_index(engine: Engine, *, clock: Callable[[], int] = utc_microseconds) -> int:
    """Build and atomically activate a complete new generation under a write gate.

    This deliberately holds ``BEGIN IMMEDIATE`` throughout. Operators must
    schedule it as maintenance for populated databases and budget free disk for
    two generations. A failure rolls back the whole build and never exposes a
    partial generation.
    """

    from patchouli_lib.database import immediate_transaction

    with immediate_transaction(engine) as connection:
        metadata = _meta(connection)
        if metadata is None:
            raise SearchIndexUnavailableError("Search index schema is missing.")
        old_generation = metadata[0]
        created_at = clock()
        if type(created_at) is not int or created_at < 0:
            raise ValueError("Search rebuild clock must return UTC microseconds.")
        result = connection.exec_driver_sql(
            "INSERT INTO search_generations (index_version, state, created_at, completed_at) "
            "VALUES (?, 'building', ?, NULL)",
            (INDEX_VERSION, created_at),
        )
        if result.lastrowid is None:
            raise SearchIndexProjectionError("Search generation identity was not allocated.")
        generation = result.lastrowid
        pages = connection.exec_driver_sql(
            "SELECT library_id, page_uid FROM pages WHERE deleted_at IS NULL "
            "ORDER BY library_id, page_uid"
        ).all()
        for library_id, page_uid in pages:
            _project_page(connection, generation, library_id, page_uid)
        indexed = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM search_page_state WHERE generation = ?", (generation,)
        ).scalar_one()
        if indexed != len(pages):
            raise SearchIndexProjectionError("Search rebuild omitted a current Page.")
        missing_terms = connection.exec_driver_sql(
            "SELECT 1 FROM search_documents AS d "
            "LEFT JOIN search_terms AS t ON t.rowid = d.id "
            "WHERE d.generation = ? AND t.rowid IS NULL LIMIT 1",
            (generation,),
        ).first()
        if missing_terms is not None:
            raise SearchIndexProjectionError("Search rebuild omitted a field index row.")
        completed_at = clock()
        if type(completed_at) is not int or completed_at < created_at:
            raise ValueError("Search rebuild completion time is invalid.")
        connection.exec_driver_sql(
            "UPDATE search_generations SET state = 'ready', completed_at = ? "
            "WHERE generation = ? AND state = 'building'",
            (completed_at, generation),
        )
        connection.exec_driver_sql(
            "UPDATE search_meta SET active_generation = ?, ready = 1, index_version = ? "
            "WHERE singleton = 1",
            (generation, INDEX_VERSION),
        )
        connection.exec_driver_sql("DELETE FROM search_dirty_pages")
        if old_generation is not None:
            old_documents = (
                connection.exec_driver_sql(
                    "SELECT id FROM search_documents WHERE generation = ?", (old_generation,)
                )
                .scalars()
                .all()
            )
            for document_id in old_documents:
                connection.exec_driver_sql(
                    "DELETE FROM search_terms WHERE rowid = ?", (document_id,)
                )
            connection.exec_driver_sql(
                "DELETE FROM search_documents WHERE generation = ?", (old_generation,)
            )
            connection.exec_driver_sql(
                "DELETE FROM search_page_state WHERE generation = ?", (old_generation,)
            )
            connection.exec_driver_sql(
                "UPDATE search_generations SET state = 'retired' WHERE generation = ?",
                (old_generation,),
            )
        return generation


__all__ = [
    "INDEX_VERSION",
    "SearchIndexProjectionError",
    "SearchIndexState",
    "SearchIndexUnavailableError",
    "dirty_sequence_at_transaction_start",
    "flush_dirty_since",
    "rebuild_search_index",
    "require_ready_index",
]
