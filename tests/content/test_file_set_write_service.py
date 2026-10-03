"""Internal file-set orchestration stays atomic, scoped and secret-safe."""

from __future__ import annotations

import json
from collections.abc import Iterator
from inspect import signature
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.models import AuditEvent
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewSectionGrant, SectionAction
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWriteNotFoundError,
    FileSetWritePreconditionRequiredError,
    FileSetWriteReplay,
    FileSetWriteService,
    FileSetWriteSuccess,
    FileSetWriteTransactionRequiredError,
)
from patchouli_lib.content.models import PageSource, Revision, RevisionFile
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.idempotency.service import IdempotencyConflictError
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewSection

from .conftest import OPERATION_TIME, ArchiveScope
from .helpers import insert_page_graph, page_graph_values

FIRST_REVISION = f"rev_{'a' * 32}"
SECOND_REVISION = f"rev_{'b' * 32}"
LEGACY_CONTENT = b"# Synthetic archive\r\n\r\nExact bytes.\n"


def _seed(engine: Engine, scope: ArchiveScope) -> tuple[str, str]:
    values = page_graph_values(
        library_id=scope.library_id,
        section_id=scope.section_id,
        book_id=scope.book_id,
    )
    page, revision, *_ = values
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, values)
    return page.page_id, page_current_etag(
        page.page_uid,
        revision.revision_id,
        revision.revision_number,
        page.occurred_at,
        page.updated_at,
    )


def _command(
    scope: ArchiveScope,
    page_id: str,
    etag: str | None,
    *,
    files: tuple[tuple[str, bytes], ...],
    section_id: str | None = None,
    source: ArchiveSourceInput | None = None,
    request_digit: str = "a",
) -> FileSetAppendCommand:
    return FileSetAppendCommand(
        library_id=scope.library_id,
        section_id=section_id or scope.section_id,
        page_id=page_id,
        expected_etag=etag,
        files=files,
        source=source or ArchiveSourceInput(kind="synthetic", locator="urn:synthetic:file-set"),
        request_id=f"req_{request_digit * 32}",
    )


def _key(name: str) -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(f"synthetic-{name}"))


def _service(
    connection: Connection,
    *,
    id_values: Iterator[str] | None = None,
    revision_values: Iterator[str] | None = None,
) -> FileSetWriteService:
    ids = id_values or iter(("6" * 32, "7" * 32))
    revisions = revision_values or iter((FIRST_REVISION,))
    return FileSetWriteService(
        connection,
        clock=lambda: OPERATION_TIME,
        id_factory=lambda: next(ids),
        revision_id_factory=lambda: next(revisions),
    )


def _counts(connection: Connection) -> tuple[int, int, int, int]:
    counts = [
        connection.scalar(select(func.count()).select_from(table)) or 0
        for table in (Revision, PageSource, AuditEvent, IdempotencyRecord)
    ]
    return counts[0], counts[1], counts[2], counts[3]


def test_multi_file_append_replay_and_identical_noop(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    files = (("figure.png", b"\x89PNG\x00\xff"), ("content.md", LEGACY_CONTENT))
    command = _command(archive_scope, page_id, etag, files=files)
    with immediate_transaction(content_engine) as connection:
        result = _service(connection).append_existing_page(
            archive_scope.token.value, command, _key("first")
        )
        assert isinstance(result, FileSetWriteSuccess)
        assert result.changed
        assert result.audit_event is not None
        assert result.page.current_revision_id == FIRST_REVISION
        body = json.loads(result.response.response_body)
        assert body["files"] == [
            {
                "filename": entry.name,
                "size_bytes": entry.content_size_bytes,
                "content_sha256": entry.content_sha256.hex(),
            }
            for entry in result.manifest.files
        ]
        assert body["snapshot_sha256"] == result.manifest.snapshot_sha256.hex()
        assert archive_scope.token.value.encode() not in result.response.response_body
        assert _counts(connection) == (2, 2, 1, 1)

        replay = _service(
            connection, id_values=iter(()), revision_values=iter(())
        ).append_existing_page(
            archive_scope.token.value,
            _command(archive_scope, page_id, etag, files=tuple(reversed(files))),
            _key("first"),
        )
        assert isinstance(replay, FileSetWriteReplay)
        assert replay.response.response_body == result.response.response_body
        assert replay.response.response_etag == result.response.response_etag
        assert _counts(connection) == (2, 2, 1, 1)

        noop = _service(
            connection, id_values=iter(()), revision_values=iter(())
        ).append_existing_page(
            archive_scope.token.value,
            _command(archive_scope, page_id, result.response.response_etag, files=files),
            _key("noop"),
        )
        assert isinstance(noop, FileSetWriteSuccess)
        assert not noop.changed
        assert noop.audit_event is None
        assert noop.response.response_etag == result.response.response_etag
        assert json.loads(noop.response.response_body)["changed"] is False
        assert json.loads(noop.response.response_body)["files"] == body["files"]
        assert _counts(connection) == (2, 2, 1, 2)

    with content_engine.connect() as connection:
        files_stored = connection.execute(
            select(RevisionFile.filename, RevisionFile.content_bytes)
            .where(RevisionFile.revision_id == FIRST_REVISION)
            .order_by(RevisionFile.filename)
        ).all()
    assert [tuple(row) for row in files_stored] == [
        ("content.md", LEGACY_CONTENT),
        ("figure.png", b"\x89PNG\x00\xff"),
    ]
    database_path = content_engine.url.database
    assert database_path is not None
    validate_database(Path(database_path))


def test_single_markdown_is_one_file_set_and_noop_on_legacy(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        result = _service(
            connection, id_values=iter(()), revision_values=iter(())
        ).append_existing_page(
            archive_scope.token.value,
            _command(archive_scope, page_id, etag, files=(("content.md", LEGACY_CONTENT),)),
            _key("single"),
        )
        assert isinstance(result, FileSetWriteSuccess)
        assert not result.changed
        assert result.page.current_revision_number == 1
        assert [entry.name for entry in result.manifest.files] == ["content.md"]
        assert _counts(connection) == (1, 1, 0, 1)

        changed = _service(connection).append_existing_page(
            archive_scope.token.value,
            _command(archive_scope, page_id, etag, files=(("content.md", b"# Updated\n"),)),
            _key("single-updated"),
        )
        assert isinstance(changed, FileSetWriteSuccess)
        assert changed.changed
        assert [entry.name for entry in changed.manifest.files] == ["content.md"]
        assert ContentRepository(connection).get_current_revision_storage_format(changed.page) == (
            "file_set_v1"
        )
        assert _counts(connection) == (2, 2, 1, 2)
    database_path = content_engine.url.database
    assert database_path is not None
    validate_database(Path(database_path))


def test_reused_key_binds_full_file_bytes_etag_and_source(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    original = _command(archive_scope, page_id, etag, files=(("notes.md", b"original"),))
    key = _key("bound")
    with immediate_transaction(content_engine) as connection:
        result = _service(connection).append_existing_page(archive_scope.token.value, original, key)
        assert isinstance(result, FileSetWriteSuccess)
        altered = (
            _command(archive_scope, page_id, etag, files=(("notes.md", b"altered"),)),
            _command(archive_scope, page_id, result.response.response_etag, files=original.files),
            _command(
                archive_scope,
                page_id,
                etag,
                files=original.files,
                source=ArchiveSourceInput(kind="another"),
            ),
        )
        for command in altered:
            with pytest.raises(IdempotencyConflictError):
                _service(
                    connection, id_values=iter(()), revision_values=iter(())
                ).append_existing_page(archive_scope.token.value, command, key)
        assert (
            "route_template" not in signature(FileSetWriteService.append_existing_page).parameters
        )
        routes = connection.scalars(select(IdempotencyRecord.route_template)).all()
        assert routes == ["/api/v1/sections/{section_id}/pages/{page_id}/file-revisions"]
        assert _counts(connection) == (2, 2, 1, 1)


def test_precondition_and_exact_scope_fail_before_mutation(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    wrong_section_id = "8" * 32
    missing_page_id = page_id.replace("synthetic-archive", "missing-archive")
    assert missing_page_id != page_id
    with immediate_transaction(content_engine) as connection:
        LibraryRepository(connection).add_section(
            NewSection(
                id=wrong_section_id,
                library_id=archive_scope.library_id,
                name="Other Synthetic Section",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        for candidate in (page_id, missing_page_id):
            with pytest.raises(AuthorizationError):
                _service(connection).append_existing_page(
                    archive_scope.token.value,
                    _command(
                        archive_scope,
                        candidate,
                        etag,
                        files=(("content.md", b"changed"),),
                        section_id=wrong_section_id,
                    ),
                    _key("ungranted-section"),
                )
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=archive_scope.library_id,
                caller_id=archive_scope.caller_id,
                section_id=wrong_section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME - 1_000_000,
            )
        )
        for candidate in (page_id, missing_page_id):
            with pytest.raises(FileSetWriteNotFoundError):
                _service(connection).append_existing_page(
                    archive_scope.token.value,
                    _command(
                        archive_scope,
                        candidate,
                        etag,
                        files=(("content.md", b"changed"),),
                        section_id=wrong_section_id,
                    ),
                    _key("wrong-section"),
                )
        with pytest.raises(FileSetWritePreconditionRequiredError):
            _service(connection).append_existing_page(
                archive_scope.token.value,
                _command(archive_scope, page_id, None, files=(("content.md", b"changed"),)),
                _key("missing-etag"),
            )
        with pytest.raises(FileSetPreconditionFailedError):
            _service(connection).append_existing_page(
                archive_scope.token.value,
                _command(
                    archive_scope,
                    page_id,
                    '"page-v2-' + "0" * 64 + '"',
                    files=(("content.md", b"changed"),),
                ),
                _key("stale-etag"),
            )
        for candidate in (page_id, missing_page_id):
            with pytest.raises(AuthenticationError):
                _service(connection).append_existing_page(
                    "invalid-bearer",
                    _command(archive_scope, candidate, etag, files=(("content.md", b"changed"),)),
                    _key("invalid-bearer"),
                )
        assert _counts(connection) == (1, 1, 0, 0)


def test_audit_failure_rolls_back_full_snapshot_even_if_caller_commits(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        first = _service(connection).append_existing_page(
            archive_scope.token.value,
            _command(archive_scope, page_id, etag, files=(("first.bin", b"one"),)),
            _key("first"),
        )
        assert isinstance(first, FileSetWriteSuccess)
        first_counts = _counts(connection)
        with pytest.raises(IntegrityError):
            _service(
                connection,
                id_values=iter(("8" * 32, first.audit_event.id if first.audit_event else "7" * 32)),
                revision_values=iter((SECOND_REVISION,)),
            ).append_existing_page(
                archive_scope.token.value,
                _command(
                    archive_scope,
                    page_id,
                    first.response.response_etag,
                    files=(("second.bin", b"two"),),
                ),
                _key("second"),
            )
        assert _counts(connection) == first_counts
        page = ContentRepository(connection).get_page(archive_scope.library_id, page_id)
        assert page is not None
        assert page.current_revision_id == FIRST_REVISION
    with content_engine.connect() as connection:
        assert _counts(connection) == first_counts


def test_replay_revalidates_presented_credential_and_current_grant(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    command = _command(archive_scope, page_id, etag, files=(("content.md", LEGACY_CONTENT),))
    with immediate_transaction(content_engine) as connection:
        original = _service(
            connection, id_values=iter(()), revision_values=iter(())
        ).append_existing_page(archive_scope.token.value, command, _key("replay-grant"))
        assert isinstance(original, FileSetWriteSuccess)
        repository = AuthRepository(connection)
        assert repository.remove_grant(
            archive_scope.library_id,
            archive_scope.caller_id,
            archive_scope.section_id,
            SectionAction.ARCHIVE_WRITE,
        )
        with pytest.raises(AuthorizationError):
            _service(connection, id_values=iter(()), revision_values=iter(())).append_existing_page(
                archive_scope.token.value, command, _key("replay-grant")
            )


def test_requires_real_caller_owned_sqlite_transaction(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    page_id, etag = _seed(content_engine, archive_scope)
    command = _command(archive_scope, page_id, etag, files=(("content.md", LEGACY_CONTENT),))
    with content_engine.connect() as connection:
        with pytest.raises(FileSetWriteTransactionRequiredError):
            _service(connection).append_existing_page(
                archive_scope.token.value, command, _key("no-transaction")
            )
        connection.exec_driver_sql("SELECT 1")
        assert connection.in_transaction()
        with pytest.raises(FileSetWriteTransactionRequiredError):
            _service(connection).append_existing_page(
                archive_scope.token.value, command, _key("autobegin")
            )
