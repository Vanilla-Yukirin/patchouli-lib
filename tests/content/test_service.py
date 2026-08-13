from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, func, select

from patchouli_lib.auth.models import AuditEvent, SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewCredential, NewSectionGrant, SectionAction
from patchouli_lib.auth.service import AuthorizationError
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.content.models import (
    Page,
    PageIdCollisionCounter,
    PageIdentifier,
    PageRevisionAppendGuard,
    PageSource,
    Revision,
)
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveIdempotencyKey,
    ArchiveMutationReplay,
    ArchiveMutationSuccess,
    ArchiveSourceInput,
    CreateArchiveCommand,
)
from patchouli_lib.content.service import (
    ArchiveNotFoundError,
    ArchivePersistenceError,
    ArchivePreconditionFailedError,
    ArchivePreconditionRequiredError,
    ArchiveService,
    ArchiveTransactionRequiredError,
)
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.models import IdempotencyRecord
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.idempotency.service import (
    IdempotencyConflictError,
    IdempotencyPersistenceError,
)
from patchouli_lib.identifiers import (
    IdentifierGenerationError,
    OccurrenceTime,
    canonical_utc_wire,
    generate_page_id,
)
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewSection

from .conftest import OPERATION_TIME, ArchiveScope
from .helpers import seed_library_structure

OCCURRED_AT = OPERATION_TIME - 876_544


def _create_command(
    scope: ArchiveScope,
    *,
    content: bytes = b"# Synthetic archive\r\n\r\nExact bytes.\n",
    title: str = "Synthetic Archive",
    request_digit: str = "a",
    book_id: str | None = None,
    section_id: str | None = None,
    source: ArchiveSourceInput | None = None,
) -> CreateArchiveCommand:
    return CreateArchiveCommand(
        library_id=scope.library_id,
        section_id=section_id or scope.section_id,
        book_id=book_id or scope.book_id,
        title=title,
        occurred_at=OCCURRED_AT,
        content_md=content,
        source=source or ArchiveSourceInput(kind="synthetic"),
        request_id=f"req_{request_digit * 32}",
    )


def _key(label: str = "create") -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(f"synthetic-{label}-key"))


def _service(
    connection: Connection,
    *,
    page_uids: Iterator[bytes] | None = None,
    revision_ids: Iterator[str] | None = None,
    opaque_ids: Iterator[str] | None = None,
) -> ArchiveService:
    page_uid_values = page_uids or iter((bytes.fromhex("aa" * 16),))
    revision_values = revision_ids or iter((f"rev_{'b' * 32}",))
    opaque_values = opaque_ids or iter(("c" * 32, "d" * 32, "e" * 32))
    return ArchiveService(
        connection,
        clock=lambda: OPERATION_TIME,
        page_uid_factory=lambda: next(page_uid_values),
        revision_id_factory=lambda: next(revision_values),
        id_factory=lambda: next(opaque_values),
    )


def _create_once(
    connection: Connection,
    scope: ArchiveScope,
    *,
    key: ArchiveIdempotencyKey | None = None,
    command: CreateArchiveCommand | None = None,
) -> ArchiveMutationSuccess:
    result = _service(connection).create_archive(
        scope.token.value,
        command or _create_command(scope),
        key or _key(),
    )
    assert isinstance(result, ArchiveMutationSuccess)
    return result


def _counts(connection: Connection) -> tuple[int, int, int, int, int, int]:
    tables = (Page, Revision, PageIdentifier, PageSource, AuditEvent, IdempotencyRecord)
    values = tuple(
        connection.execute(select(func.count()).select_from(table)).scalar_one() for table in tables
    )
    assert len(values) == 6
    return values


def _route_section_variants(
    content_engine: Engine,
    scope: ArchiveScope,
) -> tuple[str, str, str]:
    wrong_section_id = "6" * 32
    with immediate_transaction(content_engine) as connection:
        LibraryRepository(connection).add_section(
            NewSection(
                id=wrong_section_id,
                library_id=scope.library_id,
                name="Alternate Synthetic Section",
                created_at=OPERATION_TIME - 1_000_000,
                updated_at=OPERATION_TIME - 1_000_000,
            )
        )
    _, cross_library_section_id, _ = seed_library_structure(
        content_engine,
        prefix="a",
        label="Other",
    )
    return wrong_section_id, "9" * 32, cross_library_section_id


def test_create_archive_persists_exact_atomic_graph_and_safe_replay(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    command = _create_command(
        archive_scope,
        source=ArchiveSourceInput(
            kind="synthetic",
            locator="urn:synthetic:archive",
            captured_at=OCCURRED_AT,
        ),
    )
    key = _key()
    with immediate_transaction(content_engine) as connection:
        result = _service(connection).create_archive(archive_scope.token.value, command, key)
        assert isinstance(result, ArchiveMutationSuccess)
        assert result.page.section_id == archive_scope.section_id
        assert result.page.book_id == archive_scope.book_id
        assert result.page.page_type == "archive"
        assert result.page.current_revision_number == 1
        assert result.revision.content_md == command.content_md
        assert result.source is not None
        assert result.source.revision_id == result.revision.revision_id
        assert result.source.revision_number == 1
        assert result.citation.revision_id == result.revision.revision_id
        assert result.response.response_status == 201
        assert result.response.response_etag.startswith('"page-v1-')
        assert result.response.response_location == (
            f"/api/v1/sections/{archive_scope.section_id}/pages/{result.page.page_id}"
        )
        assert result.response.response_location != result.citation.href
        wire = json.loads(result.response.response_body)
        assert wire["revision"]["content"] == command.content_md.decode("utf-8")
        assert wire["revision"]["content_type"] == "text/markdown;charset=utf-8"
        assert wire["citation"]["href"].endswith("/revisions/1")
        assert "urn:synthetic:archive" not in result.response.response_body.decode("utf-8")
        assert result.audit_event.actor_caller_id == archive_scope.caller_id
        assert result.audit_event.actor_credential_id == archive_scope.credential_id
        assert result.audit_event.action == "content.archive.create"
        assert result.audit_event.resource_id == result.page.page_id
        assert _counts(connection) == (1, 1, 1, 1, 1, 1)

    with content_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        row = connection.execute(select(IdempotencyRecord.__table__)).mappings().one()
        assert "key" not in row
        assert "token" not in row
        assert "content_md" not in row
        assert archive_scope.token.value.encode() not in bytes(row["response_body"])

    with immediate_transaction(content_engine) as connection:
        replay = _service(connection).create_archive(
            archive_scope.token.value,
            command.model_copy(update={"request_id": f"req_{'f' * 32}"}),
            key,
        )
        assert isinstance(replay, ArchiveMutationReplay)
        assert replay.response.original_request_id == command.request_id
        assert replay.response.response_body == result.response.response_body
        assert replay.response.presentation_headers()["Idempotency-Replayed"] == "true"
        assert _counts(connection) == (1, 1, 1, 1, 1, 1)


def test_same_key_changed_semantics_conflicts_without_second_mutation(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    key = _key()
    with immediate_transaction(content_engine) as connection:
        _create_once(connection, archive_scope, key=key)
    with immediate_transaction(content_engine) as connection:
        with pytest.raises(IdempotencyConflictError, match="different request"):
            _service(connection).create_archive(
                archive_scope.token.value,
                _create_command(archive_scope, content=b"# Changed synthetic bytes\n"),
                key,
            )
        assert _counts(connection) == (1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("missing", ["book", "library"])
def test_missing_or_wrong_book_creates_no_rows(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    missing: str,
) -> None:
    command = _create_command(archive_scope, book_id="9" * 32)
    if missing == "library":
        command = command.model_copy(update={"library_id": "8" * 32})
    with immediate_transaction(content_engine) as connection:
        with pytest.raises(ArchiveNotFoundError, match="not found"):
            _service(connection).create_archive(archive_scope.token.value, command, _key(missing))
        assert _counts(connection) == (0, 0, 0, 0, 0, 0)


def test_create_route_section_must_match_book_without_mutation(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    sections = _route_section_variants(content_engine, archive_scope)
    for label, section_id in zip(
        ("wrong", "missing", "cross-library"),
        sections,
        strict=True,
    ):
        command = _create_command(archive_scope, section_id=section_id)
        with immediate_transaction(content_engine) as connection:
            with pytest.raises(ArchiveNotFoundError, match="not found"):
                _service(connection).create_archive(
                    archive_scope.token.value,
                    command,
                    _key(f"create-{label}-section"),
                )
            assert _counts(connection) == (0, 0, 0, 0, 0, 0)


def test_create_same_key_changed_route_section_is_a_mismatch(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    wrong_section_id, _, _ = _route_section_variants(content_engine, archive_scope)
    command = _create_command(archive_scope)
    key = _key("create-route")
    with immediate_transaction(content_engine) as connection:
        _create_once(connection, archive_scope, command=command, key=key)
    with immediate_transaction(content_engine) as connection:
        with pytest.raises(IdempotencyConflictError, match="different request"):
            _service(connection).create_archive(
                archive_scope.token.value,
                command.model_copy(update={"section_id": wrong_section_id}),
                key,
            )
        assert _counts(connection) == (1, 1, 1, 1, 1, 1)


def test_page_id_collision_consumes_registry_ordinal_without_source_dedup(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    command = _create_command(
        archive_scope,
        source=ArchiveSourceInput(kind="synthetic", locator="urn:synthetic:same"),
    )
    with immediate_transaction(content_engine) as connection:
        first = _service(connection).create_archive(archive_scope.token.value, command, _key("one"))
        second = _service(
            connection,
            page_uids=iter((bytes.fromhex("ab" * 16),)),
            revision_ids=iter((f"rev_{'d' * 32}",)),
            opaque_ids=iter(("e" * 32, "f" * 32)),
        ).create_archive(
            archive_scope.token.value,
            command.model_copy(update={"request_id": f"req_{'b' * 32}"}),
            _key("two"),
        )
        assert isinstance(first, ArchiveMutationSuccess)
        assert isinstance(second, ArchiveMutationSuccess)
        assert first.page.collision_ordinal == 1
        assert second.page.collision_ordinal == 2
        assert second.page.page_id == f"{first.page.page_id}-2"
        assert _counts(connection) == (2, 2, 2, 2, 2, 2)


def test_random_identifier_collisions_regenerate_and_bounded_failure_rolls_back(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    command = _create_command(archive_scope)
    with immediate_transaction(content_engine) as connection:
        first = _create_once(connection, archive_scope, command=command, key=_key("first"))
    replacement_uid = bytes.fromhex("ac" * 16)
    replacement_revision = f"rev_{'d' * 32}"
    with immediate_transaction(content_engine) as connection:
        second = _service(
            connection,
            page_uids=iter((first.page.page_uid, replacement_uid)),
            revision_ids=iter((first.revision.revision_id, replacement_revision)),
            opaque_ids=iter(("e" * 32, "f" * 32)),
        ).create_archive(
            archive_scope.token.value,
            command.model_copy(update={"request_id": f"req_{'b' * 32}"}),
            _key("second"),
        )
        assert isinstance(second, ArchiveMutationSuccess)
        assert second.page.page_uid == replacement_uid
        assert second.revision.revision_id == replacement_revision

    with (
        pytest.raises(IdentifierGenerationError, match="generation failed"),
        immediate_transaction(content_engine) as connection,
    ):
        ArchiveService(
            connection,
            clock=lambda: OPERATION_TIME,
            page_uid_factory=lambda: first.page.page_uid,
            revision_id_factory=lambda: first.revision.revision_id,
            id_factory=lambda: "f" * 32,
            collision_attempts=2,
        ).create_archive(
            archive_scope.token.value,
            command.model_copy(update={"request_id": f"req_{'c' * 32}"}),
            _key("third"),
        )
    with content_engine.connect() as connection:
        assert _counts(connection) == (2, 2, 2, 2, 2, 2)


def test_revision_preconditions_append_once_keep_old_and_replay_after_advance(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    with immediate_transaction(content_engine) as connection:
        created = _create_once(connection, archive_scope)
    missing = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=created.page.page_id,
        source=ArchiveSourceInput(kind="synthetic revision", locator="urn:synthetic:revision-2"),
        content_md=b"# Revision two\n",
        request_id=f"req_{'b' * 32}",
    )
    with (
        immediate_transaction(content_engine) as connection,
        pytest.raises(ArchivePreconditionRequiredError, match="ETag"),
    ):
        _service(connection).append_revision(archive_scope.token.value, missing, _key("missing"))
    stale = missing.model_copy(update={"expected_etag": '"page-v1-' + ("0" * 64) + '"'})
    with (
        immediate_transaction(content_engine) as connection,
        pytest.raises(ArchivePreconditionFailedError, match="does not match"),
    ):
        _service(connection).append_revision(archive_scope.token.value, stale, _key("stale"))

    revise = missing.model_copy(update={"expected_etag": created.response.response_etag})
    with immediate_transaction(content_engine) as connection:
        revised = _service(
            connection,
            revision_ids=iter((f"rev_{'e' * 32}",)),
            opaque_ids=iter(("d" * 32, "e" * 32)),
        ).append_revision(archive_scope.token.value, revise, _key("revise"))
        assert isinstance(revised, ArchiveMutationSuccess)
        assert revised.page.current_revision_number == 2
        assert revised.revision.revision_number == 2
        assert revised.source.revision_id == revised.revision.revision_id
        assert revised.source.revision_number == 2
        assert revised.source.kind == revise.source.kind
        assert revised.source.locator == revise.source.locator
        assert revised.response.response_location == revised.citation.href
        old = connection.execute(
            select(Revision.content_md).where(
                Revision.library_id == archive_scope.library_id,
                Revision.revision_id == created.revision.revision_id,
            )
        ).scalar_one()
        assert old == created.revision.content_md

    changed_source = revise.model_copy(
        update={
            "source": ArchiveSourceInput(
                kind="different synthetic revision",
                locator="urn:synthetic:changed-revision-source",
            )
        }
    )
    with (
        immediate_transaction(content_engine) as connection,
        pytest.raises(IdempotencyConflictError, match="different request"),
    ):
        _service(connection).append_revision(
            archive_scope.token.value,
            changed_source,
            _key("revise"),
        )

    with immediate_transaction(content_engine) as connection:
        replay = _service(connection).append_revision(
            archive_scope.token.value,
            revise,
            _key("revise"),
        )
        assert isinstance(replay, ArchiveMutationReplay)
        assert replay.body.page.current_revision_number == 2
        assert _counts(connection) == (1, 2, 1, 2, 2, 2)
        create_replay = _service(connection).create_archive(
            archive_scope.token.value,
            _create_command(archive_scope),
            _key(),
        )
        assert isinstance(create_replay, ArchiveMutationReplay)
        assert create_replay.body.page.current_revision_number == 1
        assert create_replay.response.response_etag == created.response.response_etag


def test_revision_route_section_must_match_page_without_mutation(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    sections = _route_section_variants(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        created = _create_once(connection, archive_scope)
    baseline = (1, 1, 1, 1, 1, 1)
    for label, request_digit, section_id in zip(
        ("wrong", "missing", "cross-library"),
        ("a", "b", "c"),
        sections,
        strict=True,
    ):
        command = AppendArchiveRevisionCommand(
            library_id=archive_scope.library_id,
            section_id=section_id,
            page_id=created.page.page_id,
            expected_etag=created.response.response_etag,
            source=ArchiveSourceInput(kind=f"synthetic {label} route"),
            content_md=b"# Route Section must match\n",
            request_id=f"req_{request_digit * 32}",
        )
        with immediate_transaction(content_engine) as connection:
            with pytest.raises(ArchiveNotFoundError, match="not found"):
                _service(connection).append_revision(
                    archive_scope.token.value,
                    command,
                    _key(f"revise-{label}-section"),
                )
            assert _counts(connection) == baseline


def test_revision_same_key_changed_route_section_is_a_mismatch(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    wrong_section_id, _, _ = _route_section_variants(content_engine, archive_scope)
    with immediate_transaction(content_engine) as connection:
        created = _create_once(connection, archive_scope)
    command = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=created.page.page_id,
        expected_etag=created.response.response_etag,
        source=ArchiveSourceInput(kind="synthetic route fingerprint"),
        content_md=b"# Route fingerprint\n",
        request_id=f"req_{'6' * 32}",
    )
    key = _key("revise-route")
    with immediate_transaction(content_engine) as connection:
        revised = _service(
            connection,
            revision_ids=iter((f"rev_{'6' * 32}",)),
            opaque_ids=iter(("7" * 32, "8" * 32)),
        ).append_revision(archive_scope.token.value, command, key)
        assert isinstance(revised, ArchiveMutationSuccess)
    with immediate_transaction(content_engine) as connection:
        with pytest.raises(IdempotencyConflictError, match="different request"):
            _service(connection).append_revision(
                archive_scope.token.value,
                command.model_copy(update={"section_id": wrong_section_id}),
                key,
            )
        assert _counts(connection) == (1, 2, 1, 2, 2, 2)


def test_operation_locations_match_the_accepted_typed_client_contract(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    with immediate_transaction(content_engine) as connection:
        created = _create_once(connection, archive_scope)
    expected_page_location = (
        f"/api/v1/sections/{archive_scope.section_id}/pages/{created.page.page_id}"
    )
    assert created.response.response_location == expected_page_location
    assert created.response.response_location != created.citation.href

    command = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=created.page.page_id,
        expected_etag=created.response.response_etag,
        source=ArchiveSourceInput(kind="synthetic typed-client revision"),
        content_md=b"# Typed client parity revision\n",
        request_id=f"req_{'7' * 32}",
    )
    with immediate_transaction(content_engine) as connection:
        revised = _service(
            connection,
            revision_ids=iter((f"rev_{'7' * 32}",)),
            opaque_ids=iter(("8" * 32, "9" * 32)),
        ).append_revision(archive_scope.token.value, command, _key("client-location"))
        assert isinstance(revised, ArchiveMutationSuccess)
        assert revised.response.response_location == revised.citation.href
        assert revised.response.response_location.endswith("/revisions/2")


def test_replay_rechecks_current_grant_before_lookup(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    command = _create_command(archive_scope)
    key = _key()
    with immediate_transaction(content_engine) as connection:
        _create_once(connection, archive_scope, command=command, key=key)
    with immediate_transaction(content_engine) as connection:
        repository = AuthRepository(connection)
        assert repository.remove_grant(
            archive_scope.library_id,
            archive_scope.caller_id,
            archive_scope.section_id,
            SectionAction.ARCHIVE_WRITE,
        )
        with pytest.raises(AuthorizationError, match="authorization"):
            _service(connection).create_archive(archive_scope.token.value, command, key)
    with immediate_transaction(content_engine) as connection:
        assert connection.execute(select(func.count()).select_from(SectionGrant)).scalar_one() == 0
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=archive_scope.library_id,
                caller_id=archive_scope.caller_id,
                section_id=archive_scope.section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME,
            )
        )
        replay = _service(connection).create_archive(archive_scope.token.value, command, key)
        assert isinstance(replay, ArchiveMutationReplay)


def test_replay_namespace_survives_credential_rotation_for_same_caller(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    command = _create_command(archive_scope)
    key = _key()
    with immediate_transaction(content_engine) as connection:
        created = _create_once(connection, archive_scope, command=command, key=key)

    replacement = generate_token()
    replacement_id = "6" * 32
    with immediate_transaction(content_engine) as connection:
        repository = AuthRepository(connection)
        original = repository.get_credential(
            archive_scope.library_id,
            archive_scope.caller_id,
            archive_scope.credential_id,
        )
        assert original is not None
        repository.add_credential(
            NewCredential(
                id=replacement_id,
                library_id=archive_scope.library_id,
                caller_id=archive_scope.caller_id,
                selector=replacement.selector,
                token_version=replacement.version,
                verifier=replacement.verifier,
                expires_at=OPERATION_TIME + 10_000_000,
                created_at=OPERATION_TIME,
                updated_at=OPERATION_TIME,
            )
        )
        assert (
            repository.mark_credential_rotated(
                original,
                replacement_id,
                rotated_at=OPERATION_TIME,
            )
            is not None
        )

    with immediate_transaction(content_engine) as connection:
        replay = _service(connection).create_archive(replacement.value, command, key)
        assert isinstance(replay, ArchiveMutationReplay)
        assert replay.response.response_body == created.response.response_body
        assert _counts(connection) == (1, 1, 1, 1, 1, 1)


def test_grant_added_inside_same_transaction_is_visible(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    with immediate_transaction(content_engine) as connection:
        repository = AuthRepository(connection)
        assert repository.remove_grant(
            archive_scope.library_id,
            archive_scope.caller_id,
            archive_scope.section_id,
            SectionAction.ARCHIVE_WRITE,
        )
    with immediate_transaction(content_engine) as connection:
        AuthRepository(connection).add_grant(
            NewSectionGrant(
                library_id=archive_scope.library_id,
                caller_id=archive_scope.caller_id,
                section_id=archive_scope.section_id,
                action=SectionAction.ARCHIVE_WRITE,
                created_at=OPERATION_TIME,
            )
        )
        result = _create_once(connection, archive_scope)
        assert result.page.section_id == archive_scope.section_id


@pytest.mark.parametrize(
    "failure_table",
    ["page_sources", "auth_audit_events", "idempotency_records"],
)
def test_fault_after_content_rolls_back_whole_graph(
    content_engine: Engine,
    archive_scope: ArchiveScope,
    failure_table: str,
) -> None:
    expected_error = (
        IdempotencyPersistenceError
        if failure_table == "idempotency_records"
        else ArchivePersistenceError
    )
    with (
        pytest.raises(expected_error, match="could not be persisted"),
        immediate_transaction(content_engine) as connection,
    ):
        connection.exec_driver_sql(
            f"CREATE TEMP TRIGGER fail_insert BEFORE INSERT ON {failure_table} "
            "BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END"
        )
        _create_once(connection, archive_scope)
    with content_engine.connect() as connection:
        assert _counts(connection) == (0, 0, 0, 0, 0, 0)
        assert (
            connection.execute(
                select(func.count()).select_from(PageRevisionAppendGuard)
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                select(func.count()).select_from(PageIdCollisionCounter)
            ).scalar_one()
            == 0
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_service_requires_transaction_and_never_commits(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    with content_engine.connect() as connection:
        with pytest.raises(ArchiveTransactionRequiredError, match="caller-owned"):
            _service(connection).create_archive(
                archive_scope.token.value,
                _create_command(archive_scope),
                _key(),
            )
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        _create_once(connection, archive_scope)
        assert connection.in_transaction()
        connection.rollback()
    with content_engine.connect() as connection:
        assert _counts(connection) == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "content",
    [b"invalid\x00markdown", b"\xff", b"x" * (2 * 1024 * 1024 + 1)],
    ids=("nul", "invalid-utf8", "over-limit"),
)
def test_command_rejects_invalid_exact_markdown_without_rendering_it(
    archive_scope: ArchiveScope,
    content: bytes,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _create_command(archive_scope, content=content)
    message = str(exc_info.value)
    assert repr(content) not in message
    assert "input_value" not in message


def test_secret_safe_repr_omits_body_key_and_token(archive_scope: ArchiveScope) -> None:
    content = b"unique synthetic body marker"
    command = _create_command(archive_scope, content=content)
    key = _key("private-marker")
    rendered = f"{command!r} {key!r}"
    assert content.decode() not in rendered
    assert key.key_digest.hex() not in rendered
    assert archive_scope.token.value not in rendered


def test_archive_commands_require_explicit_source_and_route_section(
    archive_scope: ArchiveScope,
) -> None:
    create_values = _create_command(archive_scope).model_dump(exclude={"source"})
    with pytest.raises(ValidationError, match="source"):
        CreateArchiveCommand.model_validate(create_values)
    create_route_values = _create_command(archive_scope).model_dump(exclude={"section_id"})
    with pytest.raises(ValidationError, match="section_id"):
        CreateArchiveCommand.model_validate(create_route_values)

    page_id = generate_page_id(
        OccurrenceTime(OCCURRED_AT, canonical_utc_wire(OCCURRED_AT)),
        "Synthetic Required Source",
    ).value
    revision_values = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=page_id,
        expected_etag='"page-v1-' + ("0" * 64) + '"',
        source=ArchiveSourceInput(kind="synthetic revision"),
        content_md=b"# Synthetic revision\n",
        request_id=f"req_{'8' * 32}",
    ).model_dump(exclude={"source"})
    with pytest.raises(ValidationError, match="source"):
        AppendArchiveRevisionCommand.model_validate(revision_values)
    revision_route_values = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=page_id,
        expected_etag='"page-v1-' + ("0" * 64) + '"',
        source=ArchiveSourceInput(kind="synthetic revision"),
        content_md=b"# Synthetic revision\n",
        request_id=f"req_{'8' * 32}",
    ).model_dump(exclude={"section_id"})
    with pytest.raises(ValidationError, match="section_id"):
        AppendArchiveRevisionCommand.model_validate(revision_route_values)


@pytest.mark.parametrize(
    "etag",
    ['W/"page-v1-' + ("0" * 64) + '"', "*", '"one", "two"'],
    ids=("weak", "wildcard", "multiple"),
)
def test_revision_command_rejects_non_single_strong_etag(
    archive_scope: ArchiveScope,
    etag: str,
) -> None:
    missing_id = generate_page_id(
        OccurrenceTime(OCCURRED_AT, canonical_utc_wire(OCCURRED_AT)),
        "Missing Synthetic Archive",
    ).value
    with pytest.raises(ValidationError, match="expected_etag"):
        AppendArchiveRevisionCommand(
            library_id=archive_scope.library_id,
            section_id=archive_scope.section_id,
            page_id=missing_id,
            expected_etag=etag,
            source=ArchiveSourceInput(kind="synthetic revision"),
            content_md=b"# Synthetic revision\n",
            request_id=f"req_{'9' * 32}",
        )


def test_revision_of_missing_page_creates_no_rows(
    content_engine: Engine,
    archive_scope: ArchiveScope,
) -> None:
    missing_id = generate_page_id(
        OccurrenceTime(OCCURRED_AT, canonical_utc_wire(OCCURRED_AT)),
        "Missing Synthetic Archive",
    ).value
    command = AppendArchiveRevisionCommand(
        library_id=archive_scope.library_id,
        section_id=archive_scope.section_id,
        page_id=missing_id,
        expected_etag='"page-v1-' + ("0" * 64) + '"',
        source=ArchiveSourceInput(kind="synthetic revision"),
        content_md=b"# Synthetic revision\n",
        request_id=f"req_{'9' * 32}",
    )
    with (
        immediate_transaction(content_engine) as connection,
        pytest.raises(ArchiveNotFoundError, match="not found"),
    ):
        _service(connection).append_revision(
            archive_scope.token.value, command, _key("missing-page")
        )
    with content_engine.connect() as connection:
        assert _counts(connection) == (0, 0, 0, 0, 0, 0)
