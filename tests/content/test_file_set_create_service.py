"""A first Page file set is complete, scoped and atomic on real Alembic head."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, func, insert, select, text
from sqlalchemy.exc import OperationalError

from patchouli_lib.auth.models import AuditEvent, CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError
from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.content.file_set_create_service import (
    FileSetCreateCommand,
    FileSetCreateNotFoundError,
    FileSetCreateReplay,
    FileSetCreateService,
    FileSetCreateSuccess,
    FileSetCreateTransactionRequiredError,
)
from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageSource,
    Revision,
    RevisionFile,
    RevisionFileSet,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.idempotency.service import IdempotencyConflictError
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewSection

from .conftest import OPERATION_TIME, ArchiveScope
from .helpers import seed_library_structure

INITIAL_REVISION = f"rev_{'a' * 32}"
NEXT_REVISION = f"rev_{'b' * 32}"
BINARY_REVISION = f"rev_{'c' * 32}"
OCCURRED_AT = OPERATION_TIME - 500_000


def _key(name: str) -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(f"synthetic-{name}"))


def _command(
    scope: ArchiveScope,
    *,
    files: tuple[tuple[str, bytes], ...],
    book_id: str | None = None,
    section_id: str | None = None,
    title: str = "Synthetic Document",
    occurred_at: int | None = OCCURRED_AT,
    source: ArchiveSourceInput | None = None,
    request_digit: str = "a",
) -> FileSetCreateCommand:
    return FileSetCreateCommand(
        library_id=scope.library_id,
        section_id=section_id or scope.section_id,
        book_id=book_id or scope.book_id,
        title=title,
        occurred_at=occurred_at,
        files=files,
        source=source or ArchiveSourceInput(kind="synthetic", locator="urn:synthetic:file-set"),
        request_id=f"req_{request_digit * 32}",
    )


def _service(
    connection: Connection,
    *,
    ids: Iterator[str] | None = None,
    revisions: Iterator[str] | None = None,
    uids: Iterator[bytes] | None = None,
) -> FileSetCreateService:
    id_values = ids if ids is not None else iter(("6" * 32, "7" * 32))
    revision_values = revisions if revisions is not None else iter((INITIAL_REVISION,))
    uid_values = uids if uids is not None else iter((bytes.fromhex("1" * 32),))
    return FileSetCreateService(
        connection,
        clock=lambda: OPERATION_TIME,
        id_factory=lambda: next(id_values),
        page_uid_factory=lambda: next(uid_values),
        revision_id_factory=lambda: next(revision_values),
    )


def _counts(connection: Connection) -> tuple[int, ...]:
    return tuple(
        connection.scalar(select(func.count()).select_from(model)) or 0
        for model in (
            Page,
            Revision,
            RevisionFileSet,
            RevisionFile,
            PageIdentifier,
            PageIdCollisionCounter,
            PageSource,
            AuditEvent,
            IdempotencyRecord,
        )
    )


def test_single_markdown_and_multi_file_share_initial_snapshot_path(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    markdown = b"# Synthetic document\n"
    with immediate_transaction(content_engine) as connection:
        single = _service(connection).create_page(
            archive_scope.token.value,
            _command(archive_scope, files=(("content.md", markdown),)),
            _key("single"),
        )
        assert isinstance(single, FileSetCreateSuccess)
        assert single.page.page_type == "archive"
        assert single.page.current_revision_number == 1
        assert single.page.occurred_at == OCCURRED_AT
        assert single.audit_event.resource_id == single.page.page_id
        single_body = json.loads(single.response.response_body)
        assert single_body["occurrence_defaulted"] is False
        assert single_body["files"] == [
            {
                "filename": "content.md",
                "size_bytes": len(markdown),
                "content_sha256": hashlib.sha256(markdown).hexdigest(),
            }
        ]
        assert _counts(connection) == (1, 1, 1, 1, 1, 1, 1, 1, 1)
        stored = connection.execute(
            select(Revision.content_md, RevisionFileSet.storage_format).join(
                RevisionFileSet,
                (RevisionFileSet.library_id == Revision.library_id)
                & (RevisionFileSet.page_uid == Revision.page_uid)
                & (RevisionFileSet.revision_id == Revision.revision_id)
                & (RevisionFileSet.revision_number == Revision.revision_number),
            )
        ).one()
        assert tuple(stored) == (None, "file_set_v1")
        assert (
            ContentRepository(connection).get_current_file_manifest(single.page) == single.manifest
        )

        multi = _service(
            connection,
            ids=iter(("8" * 32, "9" * 32)),
            revisions=iter((NEXT_REVISION,)),
            uids=iter((bytes.fromhex("2" * 32),)),
        ).create_page(
            archive_scope.token.value,
            _command(
                archive_scope,
                files=(("image.png", b"\x89PNG\x00\xff"), ("notes.md", markdown)),
                title="Another Synthetic Document",
                occurred_at=None,
            ),
            _key("multi"),
        )
        assert isinstance(multi, FileSetCreateSuccess)
        assert multi.page.current_revision_number == 1
        assert multi.page.occurred_at == OPERATION_TIME
        body = json.loads(multi.response.response_body)
        assert body["occurrence_defaulted"] is True
        assert body["files"] == [
            {
                "filename": entry.name,
                "size_bytes": entry.content_size_bytes,
                "content_sha256": entry.content_sha256.hex(),
            }
            for entry in multi.manifest.files
        ]
        assert body["snapshot_sha256"] == multi.manifest.snapshot_sha256.hex()
        assert _counts(connection) == (2, 2, 2, 3, 2, 2, 2, 2, 2)

        # The same creation title/time allocates a distinct stable Page ID;
        # a binary-only Page still uses the exact same first-Revision path.
        binary = _service(
            connection,
            ids=iter(("a" * 32, "b" * 32)),
            revisions=iter((BINARY_REVISION,)),
            uids=iter((bytes.fromhex("3" * 32),)),
        ).create_page(
            archive_scope.token.value,
            _command(archive_scope, files=(("slides.pptx", b"\x00\x01\xff"),)),
            _key("binary"),
        )
        assert isinstance(binary, FileSetCreateSuccess)
        assert binary.page.page_id != single.page.page_id
        assert binary.page.collision_ordinal == 2
        assert [entry.name for entry in binary.manifest.files] == ["slides.pptx"]
        assert _counts(connection) == (3, 3, 3, 4, 3, 2, 3, 3, 3)

    database_path = content_engine.url.database
    assert database_path is not None
    validate_database(Path(database_path))


def test_backup_rejects_initial_source_time_that_disagrees_with_create(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    with immediate_transaction(content_engine) as connection:
        created = _service(connection).create_page(
            archive_scope.token.value,
            _command(archive_scope, files=(("content.md", b"# Synthetic document\n"),)),
            _key("source-time"),
        )
        assert isinstance(created, FileSetCreateSuccess)
    database_path = content_engine.url.database
    assert database_path is not None
    validate_database(Path(database_path))
    with immediate_transaction(content_engine) as connection:
        connection.execute(
            text(
                "UPDATE page_sources SET created_at = :wrong_time "
                "WHERE library_id = :library_id AND page_uid = :page_uid"
            ),
            {
                "wrong_time": OPERATION_TIME + 1,
                "library_id": archive_scope.library_id,
                "page_uid": created.page.page_uid,
            },
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(Path(database_path))


def test_exact_replay_has_identical_response_and_does_not_allocate_another_page(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    command = _command(
        archive_scope,
        files=(("image.png", b"\x89PNG"), ("notes.md", b"Same bytes")),
    )
    key = _key("replay")
    with immediate_transaction(content_engine) as connection:
        created = _service(connection).create_page(archive_scope.token.value, command, key)
        assert isinstance(created, FileSetCreateSuccess)
        first_counts = _counts(connection)
        replay = _service(connection, ids=iter(()), revisions=iter(()), uids=iter(())).create_page(
            archive_scope.token.value,
            _command(archive_scope, files=tuple(reversed(command.files)), request_digit="b"),
            key,
        )
        assert isinstance(replay, FileSetCreateReplay)
        assert replay.response.response_body == created.response.response_body
        assert replay.response.response_etag == created.response.response_etag
        assert replay.response.original_request_id == created.response.original_request_id
        assert _counts(connection) == first_counts
        for altered in (
            _command(archive_scope, files=(("notes.md", b"changed"),)),
            _command(archive_scope, files=command.files, title="Changed title"),
            _command(
                archive_scope,
                files=command.files,
                source=ArchiveSourceInput(kind="other"),
            ),
        ):
            with pytest.raises(IdempotencyConflictError):
                _service(connection, ids=iter(()), revisions=iter(()), uids=iter(())).create_page(
                    archive_scope.token.value, altered, key
                )
        assert _counts(connection) == first_counts


def test_auth_precedes_book_lookup_and_rejects_wrong_relationship(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    other_section_id = "8" * 32
    with immediate_transaction(content_engine) as connection:
        LibraryRepository(connection).add_section(
            NewSection(
                id=other_section_id,
                library_id=archive_scope.library_id,
                name="Other Synthetic Section",
                created_at=1_000_000,
                updated_at=1_000_000,
            )
        )
        missing_book = _command(archive_scope, files=(("content.md", b"safe"),), book_id="9" * 32)
        ungranted = _command(
            archive_scope,
            files=(("content.md", b"safe"),),
            section_id=other_section_id,
        )
        before = _counts(connection)
        with pytest.raises(AuthenticationError):
            _service(connection).create_page("invalid-token", missing_book, _key("auth"))
        with pytest.raises(AuthorizationError):
            _service(connection).create_page(archive_scope.token.value, ungranted, _key("grant"))
        with pytest.raises(FileSetCreateNotFoundError):
            _service(connection).create_page(
                archive_scope.token.value, missing_book, _key("missing")
            )
        assert _counts(connection) == before


def test_revoked_credential_cannot_replay_an_earlier_creation(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    command = _command(archive_scope, files=(("content.md", b"safe"),))
    key = _key("revocation")
    with immediate_transaction(content_engine) as connection:
        result = _service(connection).create_page(archive_scope.token.value, command, key)
        assert isinstance(result, FileSetCreateSuccess)
        auth = AuthRepository(connection)
        stored = auth.get_credential(
            archive_scope.library_id,
            archive_scope.caller_id,
            archive_scope.credential_id,
        )
        assert stored is not None
        auth.revoke_credential(stored, revoked_at=OPERATION_TIME + 1)
        before = _counts(connection)
        with pytest.raises(AuthenticationError):
            _service(connection, ids=iter(()), revisions=iter(()), uids=iter(())).create_page(
                archive_scope.token.value, command, key
            )
        assert _counts(connection) == before


def test_savepoint_and_outer_rollback_leave_no_partial_page(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    command = _command(archive_scope, files=(("slides.pptx", b"\x00\x01\xff"),))
    with immediate_transaction(content_engine) as connection:
        before = _counts(connection)
        # Exhaust the second audit ID only after Page, file set and Source writes.
        with pytest.raises(StopIteration):
            _service(connection, ids=iter(("6" * 32,))).create_page(
                archive_scope.token.value, command, _key("failed")
            )
        assert _counts(connection) == before
        result = _service(connection).create_page(
            archive_scope.token.value, command, _key("failed")
        )
        assert isinstance(result, FileSetCreateSuccess)
        assert _counts(connection)[0] == 1
        connection.rollback()
    with content_engine.connect() as connection:
        assert _counts(connection) == (0,) * 9


def test_real_sqlite_transaction_required(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    command = _command(archive_scope, files=(("content.md", b"safe"),))
    with content_engine.connect() as connection:
        with pytest.raises(FileSetCreateTransactionRequiredError):
            _service(connection).create_page(archive_scope.token.value, command, _key("tx"))
        connection.exec_driver_sql("SELECT 1")
        with pytest.raises(FileSetCreateTransactionRequiredError):
            _service(connection).create_page(archive_scope.token.value, command, _key("tx"))
        assert _counts(connection) == (0,) * 9


def test_deferred_caller_acquires_writer_lock_before_authorized_reads(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    command = _command(archive_scope, files=(("content.md", b"safe"),))
    with content_engine.connect() as first, content_engine.connect() as other:
        first.exec_driver_sql("BEGIN DEFERRED")
        created = _service(first).create_page(archive_scope.token.value, command, _key("lock"))
        assert isinstance(created, FileSetCreateSuccess)
        other.exec_driver_sql("PRAGMA busy_timeout = 0")
        with pytest.raises(OperationalError, match="database is locked"):
            other.exec_driver_sql("BEGIN IMMEDIATE")
        other.rollback()
        first.rollback()
    with content_engine.connect() as connection:
        assert _counts(connection) == (0,) * 9


def test_cross_library_write_records_actor_home_and_rechecks_replay_grant(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    target_library, target_section, target_book = seed_library_structure(
        content_engine, prefix="a", label="Target"
    )
    target_scope = ArchiveScope(
        library_id=target_library,
        section_id=target_section,
        book_id=target_book,
        caller_id=archive_scope.caller_id,
        credential_id=archive_scope.credential_id,
        token=archive_scope.token,
    )
    with immediate_transaction(content_engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": archive_scope.credential_id,
                "caller_id": archive_scope.caller_id,
                "home_library_id": archive_scope.library_id,
                "mode": "library_grants",
                "created_at": OPERATION_TIME - 1,
            },
        )
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": archive_scope.credential_id,
                "caller_id": archive_scope.caller_id,
                "home_library_id": archive_scope.library_id,
                "target_library_id": target_library,
                "action": "write",
                "created_at": OPERATION_TIME - 1,
            },
        )

    command = _command(target_scope, files=(("content.md", b"cross-library"),))
    key = _key("cross-library")
    with immediate_transaction(content_engine) as connection:
        created = _service(connection).create_page(archive_scope.token.value, command, key)
        assert isinstance(created, FileSetCreateSuccess)
        assert created.audit_event.library_id == target_library
        assert created.audit_event.actor_home_library_id == archive_scope.library_id
        row = connection.execute(select(IdempotencyRecord.__table__)).mappings().one()
        assert row["library_id"] == target_library
        assert row["actor_home_library_id"] == archive_scope.library_id
        replay = _service(connection, ids=iter(()), revisions=iter(()), uids=iter(())).create_page(
            archive_scope.token.value, command, key
        )
        assert isinstance(replay, FileSetCreateReplay)
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == archive_scope.credential_id,
                CredentialLibraryGrant.target_library_id == target_library,
                CredentialLibraryGrant.action == "write",
            )
        )
        with pytest.raises(AuthorizationError):
            _service(connection, ids=iter(()), revisions=iter(()), uids=iter(())).create_page(
                archive_scope.token.value, command, key
            )
