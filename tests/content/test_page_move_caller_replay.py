"""Caller successes remain historical, but authorization follows the current Page."""

from __future__ import annotations

import json
from time import time

import pytest
from sqlalchemy import Connection, Engine, delete, insert

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.page_move_service import MasterPageMoveCommand, MasterPageMoveService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy, SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewSectionGrant, SectionAction
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand, FileSetCreateService
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.file_set_write_service import (
    FileSetAppendCommand,
    FileSetWriteNotFoundError,
    FileSetWriteService,
)
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveIdempotencyKey,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    CreateArchiveCommand,
    PageLifecycleCommand,
    PageRecord,
)
from patchouli_lib.content.service import (
    ArchiveNotFoundError,
    ArchivePreconditionFailedError,
    ArchiveReplayCorruptError,
    ArchiveService,
    page_current_etag,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import (
    OriginalResponse,
    ReplayResponse,
    digest_idempotency_key,
)
from patchouli_lib.idempotency.service import IdempotencyConflictError
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewBook, NewSection

from .conftest import OPERATION_TIME, ArchiveScope

Command = (
    CreateArchiveCommand
    | FileSetCreateCommand
    | AppendArchiveRevisionCommand
    | CorrectArchiveOccurrenceCommand
    | PageLifecycleCommand
    | FileSetAppendCommand
)
KINDS = (
    "create",
    "file-create",
    "revise",
    "correction",
    "delete",
    "restore",
    "file-changed",
    "file-noop",
)
FILES = (("notes.md", b"# complete snapshot\r\n"), ("image.bin", b"\x00\xff"))
SOURCE = ArchiveSourceInput(kind="synthetic")
REQUEST_ID = "req_" + "a" * 32
TARGET = ("8" * 32, "9" * 32)


def _key(label: str) -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(label))


def _page(engine: Engine, scope: ArchiveScope, page_id: str) -> PageRecord:
    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(scope.library_id, page_id)
        assert page is not None
        return page


def _etag(page: PageRecord) -> str:
    return page_current_etag(
        page.page_uid,
        page.current_revision_id,
        page.current_revision_number,
        page.occurred_at,
        page.updated_at,
    )


def _run(
    connection: Connection,
    scope: ArchiveScope,
    kind: str,
    command: Command,
    key: ArchiveIdempotencyKey,
    now: int,
) -> OriginalResponse:
    token = scope.token.value
    if isinstance(command, CreateArchiveCommand):
        return (
            ArchiveService(connection, clock=lambda: now)
            .create_archive(token, command, key)
            .response
        )
    if isinstance(command, FileSetCreateCommand):
        return (
            FileSetCreateService(connection, clock=lambda: now)
            .create_page(token, command, key)
            .response
        )
    if isinstance(command, AppendArchiveRevisionCommand):
        return (
            ArchiveService(connection, clock=lambda: now)
            .append_revision(token, command, key)
            .response
        )
    if isinstance(command, CorrectArchiveOccurrenceCommand):
        return ArchiveService(connection, clock=lambda: now).correct_occurrence(token, command, key)
    if isinstance(command, PageLifecycleCommand):
        return ArchiveService(connection, clock=lambda: now).transition_page_lifecycle(
            token, command, key, action="delete" if kind == "delete" else "restore"
        )
    return (
        FileSetWriteService(connection, clock=lambda: now)
        .append_existing_page(token, command, key)
        .response
    )


def _library_write(engine: Engine, scope: ArchiveScope) -> None:
    with immediate_transaction(engine) as connection:
        policy = dict(
            credential_id=scope.credential_id,
            caller_id=scope.caller_id,
            home_library_id=scope.library_id,
            created_at=OPERATION_TIME - 1,
        )
        connection.execute(insert(CredentialLibraryPolicy), dict(policy, mode="library_grants"))
        connection.execute(
            insert(CredentialLibraryGrant),
            dict(policy, target_library_id=scope.library_id, action="write"),
        )


def _prepare(
    engine: Engine, scope: ArchiveScope, kind: str
) -> tuple[PageRecord, Command, OriginalResponse]:
    create: Command
    if kind.startswith("file-"):
        create = FileSetCreateCommand(
            library_id=scope.library_id,
            section_id=scope.section_id,
            book_id=scope.book_id,
            title="Synthetic Caller Page",
            occurred_at=OPERATION_TIME - 100,
            files=FILES,
            source=SOURCE,
            request_id=REQUEST_ID,
        )
    else:
        create = CreateArchiveCommand(
            library_id=scope.library_id,
            section_id=scope.section_id,
            book_id=scope.book_id,
            title="Synthetic Caller Page",
            occurred_at=OPERATION_TIME - 100,
            content_md=b"# legacy bytes\r\n",
            source=SOURCE,
            request_id=REQUEST_ID,
        )
    with immediate_transaction(engine) as connection:
        first = _run(
            connection,
            scope,
            kind,
            create,
            _key("initial" if kind not in ("create", "file-create") else "original"),
            OPERATION_TIME,
        )
        body = json.loads(first.response_body)
        page_id = (
            body["page_id"] if isinstance(create, FileSetCreateCommand) else body["page"]["page_id"]
        )
        page = ContentRepository(connection).get_page(scope.library_id, page_id)
        assert page is not None
    if kind in ("create", "file-create"):
        return page, create, first
    common = dict(
        library_id=scope.library_id,
        section_id=scope.section_id,
        page_id=page.page_id,
        expected_etag=_etag(page),
        request_id=REQUEST_ID,
    )
    command: Command
    if kind == "revise":
        command = AppendArchiveRevisionCommand(
            **common, content_md=b"# caller revision\n", source=SOURCE
        )
    elif kind == "correction":
        command = CorrectArchiveOccurrenceCommand(**common, occurred_at=OPERATION_TIME - 200)
    elif kind in ("delete", "restore"):
        command = PageLifecycleCommand(**common)
        if kind == "restore":
            with immediate_transaction(engine) as connection:
                _run(
                    connection, scope, "delete", command, _key("earlier-delete"), OPERATION_TIME + 1
                )
            page = _page(engine, scope, page.page_id)
            command = command.model_copy(update={"expected_etag": _etag(page)})
    else:
        command = FileSetAppendCommand(
            **common,
            files=FILES if kind == "file-noop" else (("new.bin", b"\x00\x01"),),
            source=SOURCE,
        )
    with immediate_transaction(engine) as connection:
        original = _run(connection, scope, kind, command, _key("original"), OPERATION_TIME + 2)
    page = _page(engine, scope, page.page_id)
    if kind == "delete":
        restore = PageLifecycleCommand(**common).model_copy(update={"expected_etag": _etag(page)})
        with immediate_transaction(engine) as connection:
            _run(connection, scope, "restore", restore, _key("later-restore"), OPERATION_TIME + 3)
        page = _page(engine, scope, page.page_id)
    return page, command, original


def _move(
    engine: Engine, page: PageRecord, target: tuple[str, str], key: str, session: MasterAdminSession
) -> PageRecord:
    MasterPageMoveService(engine, clock=lambda: page.updated_at + 10).move_page(
        MasterPageMoveCommand(
            library_id=page.library_id,
            page_id=page.page_id,
            source_section_id=page.section_id,
            source_book_id=page.book_id,
            target_section_id=target[0],
            target_book_id=target[1],
            expected_etag=_etag(page),
        ),
        _key(key),
        master_session=session,
    )
    with engine.connect() as connection:
        moved = ContentRepository(connection).get_page(page.library_id, page.page_id)
        assert moved is not None
        return moved


def _destination(engine: Engine, scope: ArchiveScope) -> MasterAdminSession:
    with immediate_transaction(engine) as connection:
        library = LibraryRepository(connection)
        library.add_section(
            NewSection(
                id=TARGET[0],
                library_id=scope.library_id,
                name="Synthetic destination",
                created_at=1,
                updated_at=1,
            )
        )
        library.add_book(
            NewBook(
                id=TARGET[1],
                library_id=scope.library_id,
                section_id=TARGET[0],
                name="Synthetic destination book",
                created_at=1,
                updated_at=1,
            )
        )
        state = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic caller replay master token material", now=1_000_000
        )
    return MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic_csrf",
        identity_id=state.identity_id,
        session_generation=state.session_generation,
    )


def _counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
            for table in (
                "pages",
                "revisions",
                "revision_files",
                "page_sources",
                "auth_audit_events",
                "idempotency_records",
                "page_move_events",
                "admin_master_move_receipts",
                "page_occurrence_corrections",
                "page_lifecycle_events",
            )
        )


@pytest.mark.parametrize("kind", KINDS)
def test_library_write_replays_original_after_moves_later_update_and_revocation(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str
) -> None:
    _library_write(content_engine, archive_scope)
    page, command, original = _prepare(content_engine, archive_scope, kind)
    session = _destination(content_engine, archive_scope)
    page = _move(content_engine, page, TARGET, "move-out", session)
    counts = _counts(content_engine)
    with immediate_transaction(content_engine) as connection:
        replay = _run(
            connection, archive_scope, kind, command, _key("original"), page.updated_at + 1
        )
        assert isinstance(replay, ReplayResponse)
        assert replay.model_dump(exclude={"idempotency_replayed"}) == ReplayResponse(
            **original.model_dump()
        ).model_dump(exclude={"idempotency_replayed"})
        with pytest.raises(IdempotencyConflictError):
            _run(
                connection,
                archive_scope,
                kind,
                command.model_copy(update={"section_id": TARGET[0]}),
                _key("original"),
                page.updated_at + 1,
            )
        if kind not in ("create", "file-create"):
            with pytest.raises((ArchiveNotFoundError, FileSetWriteNotFoundError)):
                _run(
                    connection,
                    archive_scope,
                    kind,
                    command,
                    _key("fresh-old-path"),
                    page.updated_at + 1,
                )
            with pytest.raises((ArchivePreconditionFailedError, FileSetPreconditionFailedError)):
                _run(
                    connection,
                    archive_scope,
                    kind,
                    command.model_copy(update={"section_id": TARGET[0]}),
                    _key("fresh-current-path-stale-etag"),
                    page.updated_at + 1,
                )
    assert _counts(content_engine) == counts
    page = _move(
        content_engine,
        page,
        (archive_scope.section_id, archive_scope.book_id),
        "move-back",
        session,
    )
    later = FileSetAppendCommand(
        library_id=page.library_id,
        section_id=page.section_id,
        page_id=page.page_id,
        expected_etag=_etag(page),
        files=(("later.bin", b"later update"),),
        source=SOURCE,
        request_id="req_" + "b" * 32,
    )
    with immediate_transaction(content_engine) as connection:
        _run(connection, archive_scope, "file-changed", later, _key("later"), page.updated_at + 1)
    page = _move(
        content_engine,
        _page(content_engine, archive_scope, page.page_id),
        TARGET,
        "move-again",
        session,
    )
    # A successful old result remains replayable after later deletion as well.
    deleted = PageLifecycleCommand(
        library_id=page.library_id,
        section_id=page.section_id,
        page_id=page.page_id,
        expected_etag=_etag(page),
        request_id="req_" + "c" * 32,
    )
    with immediate_transaction(content_engine) as connection:
        _run(
            connection, archive_scope, "delete", deleted, _key("latest-delete"), page.updated_at + 1
        )
    counts = _counts(content_engine)
    with immediate_transaction(content_engine) as connection:
        replay = _run(
            connection, archive_scope, kind, command, _key("original"), page.updated_at + 2
        )
        assert isinstance(replay, ReplayResponse)
        assert (
            replay.response_body == original.response_body
            and replay.presentation_headers()["ETag"] == original.response_etag
        )
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == archive_scope.credential_id
            )
        )
        with pytest.raises(AuthorizationError):
            _run(connection, archive_scope, kind, command, _key("original"), page.updated_at + 2)
        repository = AuthRepository(connection)
        credential = repository.get_credential(
            archive_scope.library_id, archive_scope.caller_id, archive_scope.credential_id
        )
        assert credential is not None
        repository.revoke_credential(credential, revoked_at=page.updated_at + 2)
        with pytest.raises(AuthenticationError):
            _run(connection, archive_scope, kind, command, _key("original"), page.updated_at + 3)
    assert _counts(content_engine) == counts


@pytest.mark.parametrize("kind", KINDS)
def test_legacy_grant_must_follow_current_section_without_disclosing_old_response(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str
) -> None:
    page, command, original = _prepare(content_engine, archive_scope, kind)
    page = _move(
        content_engine, page, TARGET, "move-out", _destination(content_engine, archive_scope)
    )
    counts = _counts(content_engine)
    with immediate_transaction(content_engine) as connection:
        with pytest.raises(AuthorizationError):
            _run(connection, archive_scope, kind, command, _key("original"), page.updated_at + 1)
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=archive_scope.library_id,
                caller_id=archive_scope.caller_id,
                section_id=TARGET[0],
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME,
            )
        )
        connection.execute(
            delete(SectionGrant).where(SectionGrant.section_id == archive_scope.section_id)
        )
        replay = _run(
            connection, archive_scope, kind, command, _key("original"), page.updated_at + 1
        )
        assert replay.response_body == original.response_body
    assert _counts(content_engine) == counts


def test_replay_requires_entire_chain_not_a_previously_seen_path(
    content_engine: Engine, archive_scope: ArchiveScope
) -> None:
    _library_write(content_engine, archive_scope)
    page, command, _ = _prepare(content_engine, archive_scope, "correction")
    session = _destination(content_engine, archive_scope)
    page = _move(content_engine, page, TARGET, "move-out", session)
    _move(
        content_engine,
        page,
        (archive_scope.section_id, archive_scope.book_id),
        "move-back",
        session,
    )
    with immediate_transaction(content_engine) as connection:
        connection.exec_driver_sql("DROP TRIGGER trg_page_move_events_no_update")
        connection.exec_driver_sql(
            "UPDATE page_move_events SET old_updated_at = old_updated_at - 1 WHERE sequence = 1"
        )
        with pytest.raises(ArchiveReplayCorruptError):
            _run(
                connection,
                archive_scope,
                "correction",
                command,
                _key("original"),
                page.updated_at + 20,
            )


@pytest.mark.parametrize("kind", ("create", "file-create"))
@pytest.mark.parametrize("policy", ("legacy", "library"))
def test_create_current_authorization_precedes_fingerprint_conflict(
    content_engine: Engine, archive_scope: ArchiveScope, kind: str, policy: str
) -> None:
    if policy == "library":
        _library_write(content_engine, archive_scope)
    page, command, _ = _prepare(content_engine, archive_scope, kind)
    page = _move(
        content_engine, page, TARGET, "move-out", _destination(content_engine, archive_scope)
    )
    altered = command.model_copy(update={"title": "Changed same-key request"})
    counts = _counts(content_engine)
    with immediate_transaction(content_engine) as connection:
        if policy == "legacy":
            AuthRepository(connection).add_grant(
                NewSectionGrant(
                    library_id=archive_scope.library_id,
                    caller_id=archive_scope.caller_id,
                    section_id=TARGET[0],
                    action=SectionAction.ARCHIVE_WRITE,
                    created_at=OPERATION_TIME,
                )
            )
        with pytest.raises(IdempotencyConflictError):
            _run(connection, archive_scope, kind, altered, _key("original"), page.updated_at + 1)
        if policy == "legacy":
            connection.execute(delete(SectionGrant).where(SectionGrant.section_id == TARGET[0]))
        else:
            connection.execute(
                delete(CredentialLibraryGrant).where(
                    CredentialLibraryGrant.credential_id == archive_scope.credential_id
                )
            )
        for request in (command, altered):
            with pytest.raises(AuthorizationError):
                _run(
                    connection, archive_scope, kind, request, _key("original"), page.updated_at + 1
                )
    assert _counts(content_engine) == counts
