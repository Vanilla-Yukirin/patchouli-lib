"""Internal full-snapshot writes remain behind the caller's transaction boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.backup.validation import validate_database
from patchouli_lib.content.file_set_service import (
    FileSetPageNotFoundError,
    FileSetPreconditionFailedError,
    FileSetRevisionService,
)
from patchouli_lib.content.models import Page, PageSource, Revision, RevisionFile, RevisionFileSet
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import NewPageSource
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction

from .conftest import ArchiveScope
from .helpers import insert_page_graph, page_graph_values, seed_library_structure

REVISION_TWO = f"rev_{'4' * 32}"
REVISION_THREE = f"rev_{'5' * 32}"
REVISION_FOUR = f"rev_{'6' * 32}"


def _seed(engine: Engine) -> tuple[str, str, bytes, bytes, str]:
    library_id, section_id, book_id = seed_library_structure(engine)
    values = page_graph_values(
        library_id=library_id,
        section_id=section_id,
        book_id=book_id,
    )
    page, revision, *_ = values
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    etag = page_current_etag(
        page.page_uid,
        revision.revision_id,
        revision.revision_number,
        page.occurred_at,
        page.updated_at,
    )
    return library_id, page.page_id, page.page_uid, revision.content_md, etag


def _source(
    library_id: str,
    page_uid: bytes,
    revision_id: str,
    number: int,
    *,
    source_id: str = "6" * 32,
) -> NewPageSource:
    return NewPageSource(
        library_id=library_id,
        source_id=source_id,
        page_uid=page_uid,
        revision_id=revision_id,
        revision_number=number,
        kind="synthetic",
        locator="urn:synthetic:file-set",
        created_at=3_000_000,
    )


def test_legacy_to_multifile_snapshot_and_identical_noop(content_engine: Engine) -> None:
    library_id, page_id, page_uid, legacy, original_etag = _seed(content_engine)
    files = (("content.md", legacy), ("figure.png", b"\x89PNG\x00\xff"))
    with immediate_transaction(content_engine) as connection:
        service = FileSetRevisionService(connection)
        legacy_noop = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=original_etag,
            files=(("content.md", legacy),),
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
        )
        assert not legacy_noop.changed
        assert legacy_noop.page.current_revision_number == 1
        result = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=original_etag,
            files=files,
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
        )
        assert result.changed
        assert result.page.current_revision_number == 2
        assert result.page.current_revision_id == REVISION_TWO
        assert [(file.name, file.content) for file in result.manifest.files] == list(files)
        assert result.etag != original_etag

        noop = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=result.etag,
            files=reversed(files),
            revision_id=REVISION_THREE,
            revision_at=4_000_000,
            source=_source(library_id, page_uid, REVISION_THREE, 3, source_id="7" * 32),
        )
        assert not noop.changed
        assert noop.etag == result.etag
        assert noop.page.updated_at == result.page.updated_at

    with content_engine.connect() as connection:
        revisions = connection.execute(
            select(Revision.revision_number, Revision.content_md, RevisionFileSet.storage_format)
            .join(
                RevisionFileSet,
                (RevisionFileSet.library_id == Revision.library_id)
                & (RevisionFileSet.page_uid == Revision.page_uid)
                & (RevisionFileSet.revision_id == Revision.revision_id)
                & (RevisionFileSet.revision_number == Revision.revision_number),
            )
            .where(Revision.library_id == library_id, Revision.page_uid == page_uid)
            .order_by(Revision.revision_number)
        ).all()
        sources = connection.scalar(
            select(func.count())
            .select_from(PageSource)
            .where(
                PageSource.library_id == library_id,
                PageSource.page_uid == page_uid,
            )
        )
    assert [tuple(row) for row in revisions] == [
        (1, legacy, "legacy_markdown"),
        (2, None, "file_set_v1"),
    ]
    assert sources == 2
    database_path = content_engine.url.database
    assert database_path is not None
    validate_database(Path(database_path))


def test_binary_only_then_changed_name_or_bytes_creates_revisions(content_engine: Engine) -> None:
    library_id, page_id, page_uid, _legacy, original_etag = _seed(content_engine)
    with immediate_transaction(content_engine) as connection:
        service = FileSetRevisionService(connection)
        first = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=original_etag,
            files=(("slides.pptx", b"\x00\x01\xff"),),
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
        )
        second = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=first.etag,
            files=(("renamed.pptx", b"\x00\x01\xff"),),
            revision_id=REVISION_THREE,
            revision_at=4_000_000,
            source=_source(library_id, page_uid, REVISION_THREE, 3, source_id="7" * 32),
        )
        assert first.manifest.snapshot_sha256 != second.manifest.snapshot_sha256
        assert first.etag != second.etag
        third = service.append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=second.etag,
            files=(("renamed.pptx", b"\x00\x01\xfe"),),
            revision_id=REVISION_FOUR,
            revision_at=5_000_000,
            source=_source(library_id, page_uid, REVISION_FOUR, 4, source_id="8" * 32),
        )
        assert second.manifest.snapshot_sha256 != third.manifest.snapshot_sha256
        assert second.etag != third.etag
    with content_engine.connect() as connection:
        rows = connection.execute(
            select(RevisionFile.revision_number, RevisionFile.filename, RevisionFile.content_bytes)
            .where(RevisionFile.library_id == library_id, RevisionFile.page_uid == page_uid)
            .order_by(RevisionFile.revision_number)
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, "content.md", _legacy),
        (2, "slides.pptx", b"\x00\x01\xff"),
        (3, "renamed.pptx", b"\x00\x01\xff"),
        (4, "renamed.pptx", b"\x00\x01\xfe"),
    ]


def test_stale_etag_and_missing_page_do_not_write(content_engine: Engine) -> None:
    library_id, page_id, page_uid, legacy, original_etag = _seed(content_engine)
    with immediate_transaction(content_engine) as connection:
        service = FileSetRevisionService(connection)
        with pytest.raises(FileSetPreconditionFailedError):
            service.append_existing_page(
                library_id=library_id,
                page_id=page_id,
                expected_etag='"stale"',
                files=(("content.md", legacy + b"changed"),),
                revision_id=REVISION_TWO,
                revision_at=3_000_000,
                source=_source(library_id, page_uid, REVISION_TWO, 2),
            )
        with pytest.raises(FileSetPageNotFoundError):
            service.append_existing_page(
                library_id=library_id,
                page_id=page_id.replace("synthetic-archive", "absent-page"),
                expected_etag=original_etag,
                files=(("binary.dat", b"\x00"),),
                revision_id=REVISION_TWO,
                revision_at=3_000_000,
                source=_source(library_id, page_uid, REVISION_TWO, 2),
            )
    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1


def test_source_failure_rolls_back_complete_append_even_if_caller_commits(
    content_engine: Engine,
) -> None:
    library_id, page_id, page_uid, _legacy, original_etag = _seed(content_engine)
    with immediate_transaction(content_engine) as connection:
        service = FileSetRevisionService(connection)
        with pytest.raises(IntegrityError):
            service.append_existing_page(
                library_id=library_id,
                page_id=page_id,
                expected_etag=original_etag,
                files=(("binary.dat", b"\x00\xff"),),
                revision_id=REVISION_TWO,
                revision_at=3_000_000,
                source=_source(
                    library_id,
                    page_uid,
                    REVISION_TWO,
                    2,
                    source_id="3" * 32,  # Existing Source ID; failure happens after file writes.
                ),
            )
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFileSet)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFile)) == 1
        assert connection.scalar(select(Page.current_revision_number)) == 1


def test_successful_inner_append_still_rolls_back_with_caller_transaction(
    content_engine: Engine,
) -> None:
    library_id, page_id, page_uid, _legacy, original_etag = _seed(content_engine)
    with content_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection) and raw.in_transaction
        result = FileSetRevisionService(connection).append_existing_page(
            library_id=library_id,
            page_id=page_id,
            expected_etag=original_etag,
            files=(("binary.dat", b"\x00\xff"),),
            revision_id=REVISION_TWO,
            revision_at=3_000_000,
            source=_source(library_id, page_uid, REVISION_TWO, 2),
        )
        assert result.changed
        connection.rollback()

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFileSet)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFile)) == 1
        assert connection.scalar(select(func.count()).select_from(PageSource)) == 1
        assert connection.scalar(select(Page.current_revision_number)) == 1


def test_sqlalchemy_select_autobegin_without_sqlite_begin_is_rejected(
    content_engine: Engine,
) -> None:
    library_id, page_id, page_uid, _legacy, original_etag = _seed(content_engine)
    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.in_transaction()
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection) and not raw.in_transaction
        with pytest.raises(RuntimeError, match="caller-owned SQLite write transaction"):
            FileSetRevisionService(connection).append_existing_page(
                library_id=library_id,
                page_id=page_id,
                expected_etag=original_etag,
                files=(("binary.dat", b"\x00\xff"),),
                revision_id=REVISION_TWO,
                revision_at=3_000_000,
                source=_source(library_id, page_uid, REVISION_TWO, 2),
            )
        connection.rollback()

    with content_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Revision)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFileSet)) == 1
        assert connection.scalar(select(func.count()).select_from(RevisionFile)) == 1
        assert connection.scalar(select(func.count()).select_from(PageSource)) == 1
        assert connection.scalar(select(Page.current_revision_number)) == 1


def test_deleted_page_rejected_before_writing(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    library_id = archive_scope.library_id
    values = page_graph_values(
        library_id=library_id,
        section_id=archive_scope.section_id,
        book_id=archive_scope.book_id,
    )
    page, revision, *_ = values
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
        repository = ContentRepository(connection)
        stored_page = repository.get_page(library_id, page.page_id)
        assert stored_page is not None
        repository.transition_page_lifecycle(
            stored_page,
            action="delete",
            actor_caller_id=archive_scope.caller_id,
            request_id="req_" + "a" * 32,
            changed_at=3_000_000,
        )
        service = FileSetRevisionService(connection)
        etag = page_current_etag(
            page.page_uid,
            revision.revision_id,
            1,
            page.occurred_at,
            page.updated_at,
        )
        with pytest.raises(FileSetPageNotFoundError):
            service.append_existing_page(
                library_id=library_id,
                page_id=page.page_id,
                expected_etag=etag,
                files=(("binary.dat", b"x"),),
                revision_id=REVISION_TWO,
                revision_at=3_000_000,
                source=_source(library_id, page.page_uid, REVISION_TWO, 2),
            )
