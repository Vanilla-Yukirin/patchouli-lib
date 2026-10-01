"""Role-neutral first file-set writes on a migrated synthetic database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, func, select

from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_create_core import (
    FileSetCreateTransactionRequiredError,
    FileSetPageCreateCore,
)
from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageSource,
    Revision,
    RevisionFile,
    RevisionFileSeal,
    RevisionFileSet,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveSourceInput
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord

from .conftest import OPERATION_TIME
from .helpers import seed_library_structure

_SOURCE = ArchiveSourceInput(kind="synthetic")
_OCCURRED_AT = OPERATION_TIME - 500_000


def _core(
    connection: Connection,
    *,
    ids: Iterator[str],
    uids: Iterator[bytes],
    revisions: Iterator[str],
) -> FileSetPageCreateCore:
    return FileSetPageCreateCore(
        connection,
        id_factory=lambda: next(ids),
        page_uid_factory=lambda: next(uids),
        revision_id_factory=lambda: next(revisions),
    )


def _counts(connection: Connection) -> tuple[int, ...]:
    return tuple(
        connection.scalar(select(func.count()).select_from(model)) or 0
        for model in (
            Page,
            Revision,
            RevisionFileSet,
            RevisionFile,
            RevisionFileSeal,
            PageIdentifier,
            PageIdCollisionCounter,
            PageSource,
            AuditEvent,
            IdempotencyRecord,
        )
    )


def test_core_writes_exact_complete_snapshot_without_agent_identity(content_engine: Engine) -> None:
    library_id, _, book_id = seed_library_structure(content_engine)
    manifest = build_file_manifest((("slides.pptx", b"\x00\xff"), ("notes.md", b"# Synthetic\n")))
    with immediate_transaction(content_engine) as connection:
        repository = ContentRepository(connection)
        book = repository.get_book(library_id, book_id)
        assert book is not None
        core = _core(
            connection,
            ids=iter(("6" * 32,)),
            uids=iter((bytes.fromhex("7" * 32),)),
            revisions=iter(("rev_" + "8" * 32,)),
        )
        page = core.create_page(
            book=book,
            title="Synthetic document",
            occurred_at=_OCCURRED_AT,
            operation_at=OPERATION_TIME,
            manifest=manifest,
            source=_SOURCE,
        )
        assert page.page_type == "archive"
        assert page.current_revision_number == 1
        assert page.book_id == book_id
        assert repository.get_current_file_manifest(page) == manifest
        assert _counts(connection) == (1, 1, 1, 2, 1, 1, 1, 1, 0, 0)
        assert tuple(
            connection.scalars(
                select(RevisionFile.content_bytes)
                .where(RevisionFile.library_id == library_id)
                .order_by(RevisionFile.filename)
            )
        ) == tuple(entry.content for entry in manifest.files)
        assert connection.scalar(select(Revision.content_md)) is None
        assert connection.scalar(select(RevisionFileSet.storage_format)) == "file_set_v1"


def test_core_preserves_page_id_collision_allocation(content_engine: Engine) -> None:
    library_id, _, book_id = seed_library_structure(content_engine)
    manifest = build_file_manifest((("payload.bin", b"\x00\xff"),))
    with immediate_transaction(content_engine) as connection:
        book = ContentRepository(connection).get_book(library_id, book_id)
        assert book is not None
        core = _core(
            connection,
            ids=iter(("6" * 32, "7" * 32)),
            uids=iter((bytes.fromhex("8" * 32), bytes.fromhex("9" * 32))),
            revisions=iter(("rev_" + "a" * 32, "rev_" + "b" * 32)),
        )
        pages = tuple(
            core.create_page(
                book=book,
                title="Repeated title",
                occurred_at=_OCCURRED_AT,
                operation_at=OPERATION_TIME,
                manifest=manifest,
                source=_SOURCE,
            )
            for _ in range(2)
        )
        assert tuple(page.collision_ordinal for page in pages) == (1, 2)
        assert pages[0].page_id != pages[1].page_id
        assert _counts(connection) == (2, 2, 2, 2, 2, 2, 1, 2, 0, 0)


def test_core_savepoint_and_outer_transaction_are_atomic(content_engine: Engine) -> None:
    library_id, _, book_id = seed_library_structure(content_engine)
    manifest = build_file_manifest((("content.md", b"# Synthetic\n"),))
    with immediate_transaction(content_engine) as connection:
        book = ContentRepository(connection).get_book(library_id, book_id)
        assert book is not None
        with pytest.raises(StopIteration):
            _core(
                connection,
                ids=iter(()),
                uids=iter((bytes.fromhex("8" * 32),)),
                revisions=iter(("rev_" + "a" * 32,)),
            ).create_page(
                book=book,
                title="Synthetic document",
                occurred_at=_OCCURRED_AT,
                operation_at=OPERATION_TIME,
                manifest=manifest,
                source=_SOURCE,
            )
        assert _counts(connection) == (0,) * 10
        created = _core(
            connection,
            ids=iter(("6" * 32,)),
            uids=iter((bytes.fromhex("8" * 32),)),
            revisions=iter(("rev_" + "a" * 32,)),
        ).create_page(
            book=book,
            title="Synthetic document",
            occurred_at=_OCCURRED_AT,
            operation_at=OPERATION_TIME,
            manifest=manifest,
            source=_SOURCE,
        )
        assert created.collision_ordinal == 1
        connection.rollback()
    with content_engine.connect() as connection:
        assert _counts(connection) == (0,) * 10


def test_core_rejects_missing_concrete_sqlite_transaction(content_engine: Engine) -> None:
    library_id, _, book_id = seed_library_structure(content_engine)
    manifest = build_file_manifest((("content.md", b"# Synthetic\n"),))
    with content_engine.connect() as connection:
        book = ContentRepository(connection).get_book(library_id, book_id)
        assert book is not None
        with pytest.raises(FileSetCreateTransactionRequiredError):
            _core(
                connection,
                ids=iter(()),
                uids=iter(()),
                revisions=iter(()),
            ).create_page(
                book=book,
                title="Synthetic document",
                occurred_at=_OCCURRED_AT,
                operation_at=OPERATION_TIME,
                manifest=manifest,
                source=_SOURCE,
            )
        assert _counts(connection) == (0,) * 10
