"""Synthetic real credentials, immutable snapshots and role-mixed move history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from alembic import command as alembic_command
from backup.conftest import _config
from backup.test_master_occurrence_backup import _historical_bundle, _tamper
from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.contracts import (
    MasterDeletePageFormInput,
    MasterRotateAgentCredentialInput,
)
from patchouli_lib.admin.page_move_service import MasterPageMoveService
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.auth.models import (
    AuditEvent,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, CallerKind, NewAuditEvent, NewCaller
from patchouli_lib.auth.service import AuthenticationError, AuthorizationError, CredentialIssuer
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    BackupArtifactIdentity,
    BackupDatabaseError,
    create_backup,
    restore_backup,
    validate_database,
)
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_move_core import PageMoveNotFoundError
from patchouli_lib.content.page_move_models import PageMoveGuard
from patchouli_lib.content.page_move_schemas import PageMoveBody, PageMoveCommand
from patchouli_lib.content.page_move_service import CallerPageMoveService
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.idempotency.service import IdempotencyConflictError
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import NewLibrary
from patchouli_lib.search.index_v2 import rebuild_search_index

from .conftest import OPERATION_TIME
from .page_move_helpers import _command, _target
from .test_master_revision_restore_service import Story, _etag, _key, _page, _story
from .test_page_move_migration import _guard


@dataclass(frozen=True)
class MovingAgent:
    token: str
    home: str
    caller_id: str
    credential_id: str


def _agent(
    engine: Engine,
    target_library: str,
    *,
    home_prefix: str = "7",
    opted_in: bool = True,
    write: bool = True,
    caller_id: str = "a" * 32,
) -> MovingAgent:
    home = home_prefix * 32
    credential_id = home_prefix * 32
    with immediate_transaction(engine) as connection:
        LibraryRepository(connection).add_library(
            NewLibrary(
                id=home,
                name=f"Synthetic home {home_prefix}",
                created_at=OPERATION_TIME - 1,
                updated_at=OPERATION_TIME - 1,
            )
        )
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id=caller_id,
                library_id=home,
                kind=CallerKind.AGENT,
                name="Synthetic moving Agent",
                created_at=OPERATION_TIME - 1,
                updated_at=OPERATION_TIME - 1,
            )
        )
        issued = CredentialIssuer(
            auth, clock=lambda: OPERATION_TIME - 1, id_factory=lambda: credential_id
        ).issue(caller, expires_at=OPERATION_TIME + 10_000_000)
        if opted_in:
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": credential_id,
                    "caller_id": caller_id,
                    "home_library_id": home,
                    "mode": "library_grants",
                    "created_at": OPERATION_TIME,
                },
            )
            connection.execute(
                insert(CredentialLibraryGrant),
                {
                    "credential_id": credential_id,
                    "caller_id": caller_id,
                    "home_library_id": home,
                    "target_library_id": target_library,
                    "action": "write" if write else "read",
                    "created_at": OPERATION_TIME,
                },
            )
    return MovingAgent(issued.value, home, caller_id, credential_id)


def _move_command(page: PageRecord, target: tuple[str, str]) -> PageMoveCommand:
    return PageMoveCommand(**_command(page, target).model_dump(), request_id="req_" + "e" * 32)


def _run(
    engine: Engine,
    agent: MovingAgent,
    request: PageMoveCommand,
    key: str = "move",
) -> OriginalResponse | ReplayResponse:
    with immediate_transaction(engine) as connection:
        return CallerPageMoveService(connection, clock=lambda: OPERATION_TIME + 30).move_page(
            agent.token, request, _key(key)
        )


@pytest.mark.parametrize("kind", ["legacy", "markdown", "mixed", "binary"])
@pytest.mark.parametrize("cross_section", [False, True])
def test_agent_move_preserves_full_identity_and_real_actor(
    content_engine: Engine,
    kind: str,
    cross_section: bool,
) -> None:
    story = _story(content_engine, kind)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    target = _target(content_engine, page, cross_section=cross_section)
    with content_engine.connect() as connection:
        original_files = ContentRepository(connection).get_current_file_manifest(page)
        old_revisions = connection.exec_driver_sql("SELECT * FROM revisions").all()
        old_sources = connection.exec_driver_sql("SELECT * FROM page_sources").all()
    request = _move_command(page, target)
    first = _run(content_engine, agent, request)
    assert not isinstance(first, ReplayResponse)
    moved = _page(content_engine, page.library_id, page.page_id)
    assert moved == page.model_copy(
        update={"section_id": target[0], "book_id": target[1], "updated_at": OPERATION_TIME + 30}
    )
    with content_engine.connect() as connection:
        assert ContentRepository(connection).get_current_file_manifest(moved) == original_files
        assert connection.exec_driver_sql("SELECT * FROM revisions").all() == old_revisions
        assert connection.exec_driver_sql("SELECT * FROM page_sources").all() == old_sources
        audit = (
            connection.execute(
                select(AuditEvent.__table__).where(AuditEvent.action == "content.page.move")
            )
            .mappings()
            .one()
        )
        assert (
            audit["library_id"],
            audit["actor_home_library_id"],
            audit["actor_caller_id"],
            audit["actor_credential_id"],
        ) == (page.library_id, agent.home, agent.caller_id, agent.credential_id)
        assert connection.exec_driver_sql("SELECT count(*) FROM page_move_guards").scalar_one() == 0
    replay = _run(content_engine, agent, request)
    assert isinstance(replay, ReplayResponse) and replay.response_body == first.response_body
    assert replay.response_etag == first.response_etag
    activities = AdminReadModel(content_engine).recent_content_activity(
        actor=(agent.home, agent.caller_id)
    )
    assert len(activities) == 1 and activities[0].action == "content.page.move"
    assert activities[0].actor_home_library_id == agent.home
    assert activities[0].revision_number == page.current_revision_number
    assert (activities[0].section_id, activities[0].book_id) == target


def _delete(engine: Engine, story: Story) -> None:
    page = _page(engine, story.command.library_id, story.command.page_id)
    AdminActionService(engine, clock=lambda: OPERATION_TIME + 100).delete_page_as_master(
        page.library_id,
        page.section_id,
        page.book_id,
        page.page_id,
        MasterDeletePageFormInput(expected_etag=_etag(page), confirm_delete="yes"),
        master_session=story.session,
    )


def test_noop_and_move_replay_after_mixed_moves_and_delete(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    target = _target(content_engine, page)
    noop = _move_command(page, (page.section_id, page.book_id))
    noop_response = _run(content_engine, agent, noop, "noop")
    assert not PageMoveBody.model_validate_json(noop_response.response_body).changed
    master = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 10)
    master_result = master.move_page(
        _command(page, target), _key("master-first"), master_session=story.session
    )
    moved = _page(content_engine, page.library_id, page.page_id)
    caller_request = _move_command(moved, (page.section_id, page.book_id))
    caller_response = _run(content_engine, agent, caller_request)
    returned = _page(content_engine, page.library_id, page.page_id)
    master.move_page(_command(returned, target), _key("master-last"), master_session=story.session)
    _delete(content_engine, story)
    deleted = _page(content_engine, page.library_id, page.page_id)
    assert deleted.deleted_at is not None
    assert (
        _run(content_engine, agent, caller_request).response_body == caller_response.response_body
    )
    assert _run(content_engine, agent, noop, "noop").response_body == noop_response.response_body
    assert (
        master.move_page(
            _command(page, target), _key("master-first"), master_session=story.session
        ).receipt
        == master_result.receipt
    )
    assert _page(content_engine, page.library_id, page.page_id) == deleted
    with pytest.raises(PageMoveNotFoundError):
        _run(content_engine, agent, _move_command(deleted, target), "new-after-delete")


@pytest.mark.parametrize("opted_in,write", [(False, True), (True, False)])
def test_legacy_or_read_only_cannot_move(
    content_engine: Engine, opted_in: bool, write: bool
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id, opted_in=opted_in, write=write)
    with pytest.raises(AuthorizationError):
        _run(content_engine, agent, _move_command(page, _target(content_engine, page)))
    assert _page(content_engine, page.library_id, page.page_id) == page


def test_current_auth_before_conflict_and_replay(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    request = _move_command(page, _target(content_engine, page))
    _run(content_engine, agent, request)
    changed = request.model_copy(update={"target_book_id": "f" * 32})
    with pytest.raises(IdempotencyConflictError):
        _run(content_engine, agent, changed)
    with immediate_transaction(content_engine) as connection:
        connection.execute(
            delete(CredentialLibraryGrant).where(
                CredentialLibraryGrant.credential_id == agent.credential_id
            )
        )
    for submitted in (request, changed):
        with pytest.raises(AuthorizationError):
            _run(content_engine, agent, submitted)
    with immediate_transaction(content_engine) as connection:
        connection.exec_driver_sql(
            "UPDATE auth_credentials SET revoked_at=?, updated_at=? WHERE id=?",
            (OPERATION_TIME + 1, OPERATION_TIME + 1, agent.credential_id),
        )
    with pytest.raises(AuthenticationError):
        _run(content_engine, agent, request)


def test_rotated_valid_credential_replays_same_caller_success(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    request = _move_command(page, _target(content_engine, page))
    first = _run(content_engine, agent, request)
    replacement = AdminActionService(
        content_engine, clock=lambda: OPERATION_TIME + 20
    ).rotate_agent_credential_as_master(
        agent.home,
        agent.caller_id,
        agent.credential_id,
        MasterRotateAgentCredentialInput(credential_ttl_seconds=3600),
        master_session=story.session,
    )
    with pytest.raises(AuthenticationError):
        _run(content_engine, agent, request)
    current = MovingAgent(replacement.value, agent.home, agent.caller_id, replacement.credential_id)
    replay = _run(content_engine, current, request)
    assert isinstance(replay, ReplayResponse) and replay.response_body == first.response_body
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT actor_credential_id FROM auth_audit_events WHERE action='content.page.move'"
        ).scalars().all() == [agent.credential_id]


@pytest.mark.parametrize("case", ["both", "neither", "wrong-resource"])
def test_guard_requires_one_exact_real_audit_actor(content_engine: Engine, case: str) -> None:
    values = _guard(content_engine)
    with content_engine.connect() as connection:
        page_id = connection.exec_driver_sql("SELECT page_id FROM pages").scalar_one()
    agent = _agent(content_engine, str(values["library_id"]))
    with immediate_transaction(content_engine) as connection:
        AuthRepository(connection).add_audit_event(
            NewAuditEvent(
                id="b" * 32,
                library_id=str(values["library_id"]),
                actor_home_library_id=agent.home,
                actor_caller_id=agent.caller_id,
                actor_credential_id=agent.credential_id,
                action="content.page.move",
                resource_type="page",
                resource_id=page_id if case != "wrong-resource" else "wrong",
                outcome=AuditOutcome.SUCCEEDED,
                request_id="req_" + "e" * 32,
                occurred_at=int(str(values["changed_at"])),
            )
        )
    values["caller_audit_event_id"] = None if case == "neither" else "b" * 32
    if case != "both":
        values["master_audit_event_id"] = None
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveGuard), values)


def test_wrong_path_old_etag_and_cross_library_target_are_atomic(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    target = _target(content_engine, page)
    request = _move_command(page, target)
    for update in (
        {"source_book_id": "f" * 32},
        {"target_section_id": "8" * 32, "target_book_id": "9" * 32},
    ):
        with pytest.raises(PageMoveNotFoundError):
            _run(content_engine, agent, request.model_copy(update=update), "invalid")
    with pytest.raises(FileSetPreconditionFailedError):
        _run(
            content_engine,
            agent,
            request.model_copy(update={"expected_etag": '"page-v2-' + "0" * 64 + '"'}),
        )
    assert _page(content_engine, page.library_id, page.page_id) == page


def test_distinct_home_caller_key_namespaces(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    first = _agent(content_engine, page.library_id)
    second = _agent(content_engine, page.library_id, home_prefix="b", caller_id="c" * 32)
    request = _move_command(page, (page.section_id, page.book_id))
    assert not isinstance(_run(content_engine, first, request, "same-key"), ReplayResponse)
    assert not isinstance(_run(content_engine, second, request, "same-key"), ReplayResponse)
    assert isinstance(_run(content_engine, first, request, "same-key"), ReplayResponse)
    assert isinstance(_run(content_engine, second, request, "same-key"), ReplayResponse)


def test_index_failure_rolls_back_audit_move_and_success(
    content_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    request = _move_command(page, _target(content_engine, page))
    rebuild_search_index(content_engine)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic index failure")

    monkeypatch.setattr("patchouli_lib.search.index_v2.flush_dirty_since", fail)
    with pytest.raises(RuntimeError, match="Synthetic index failure"):
        _run(content_engine, agent, request)
    assert _page(content_engine, page.library_id, page.page_id) == page
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM page_move_events").scalar_one() == 0
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM auth_audit_events WHERE action='content.page.move'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("changed", [False, True])
def test_caller_success_blocks_lossy_downgrade(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    target = _target(content_engine, page) if changed else (page.section_id, page.book_id)
    _run(content_engine, agent, _move_command(page, target))
    assert content_engine.url.database is not None
    with pytest.raises(RuntimeError, match="Cannot discard"):
        alembic_command.downgrade(
            _config(Path(content_engine.url.database), monkeypatch), "20261001_0028"
        )
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == CURRENT_SCHEMA_REVISION
        )


def test_mixed_move_bundle_restore_exact_bytes(content_engine: Engine, tmp_path: Path) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    original = _run(content_engine, agent, _move_command(page, _target(content_engine, page)))
    _delete(content_engine, story)
    with content_engine.connect() as connection:
        original_bytes = connection.exec_driver_sql(
            "SELECT revision_id, filename, content_bytes FROM revision_files "
            "ORDER BY revision_id, filename"
        ).all()
    bundle = create_backup(
        content_engine,
        tmp_path / "moves",
        app_version="0.1.0a0",
        artifact_identity=BackupArtifactIdentity("synthetic/agent-moves", "sha256:" + "1" * 64),
    )
    restored = tmp_path / "restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version="0.1.0a0")
    assert validate_database(restored).schema_revision == CURRENT_SCHEMA_REVISION
    with closing(sqlite3.connect(restored)) as connection:
        assert (
            connection.execute(
                "SELECT response_body FROM idempotency_records WHERE route_template LIKE '%/move'"
            ).fetchone()[0]
            == original.response_body
        )
        assert connection.execute(
            "SELECT revision_id, filename, content_bytes FROM revision_files "
            "ORDER BY revision_id, filename"
        ).fetchall() == [tuple(row) for row in original_bytes]


def test_0028_master_history_exact_upgrade_and_backup(
    content_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    original = MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME + 10).move_page(
        _command(page, _target(content_engine, page)),
        _key("old-master"),
        master_session=story.session,
    )
    assert content_engine.url.database is not None
    database = Path(content_engine.url.database)
    config = _config(database, monkeypatch)
    alembic_command.downgrade(config, "20261001_0028")
    assert validate_database(database, schema_revision="20261001_0028")
    with closing(sqlite3.connect(database)) as connection:
        old_events = connection.execute("SELECT * FROM page_move_events").fetchall()
        old_receipts = connection.execute("SELECT * FROM admin_master_move_receipts").fetchall()
    bundle = tmp_path / "old-master"
    manifest = replace(_historical_bundle(database, bundle), schema_revision="20261001_0028")
    (bundle / "manifest.json").write_bytes(manifest.canonical_bytes())
    restored = tmp_path / "old-restored.sqlite"
    restore_backup(bundle, restored, app_version="0.1.0a0", schema_revision="20261001_0028")
    assert restored.read_bytes() == (bundle / BACKUP_FILENAME).read_bytes()
    alembic_command.upgrade(config, "head")
    assert validate_database(database)
    with closing(sqlite3.connect(database)) as connection:
        assert [
            row[:-1] for row in connection.execute("SELECT * FROM page_move_events")
        ] == old_events
        assert (
            connection.execute("SELECT * FROM admin_master_move_receipts").fetchall()
            == old_receipts
        )
    assert (
        MasterPageMoveService(content_engine)
        .move_page(
            _command(page, (original.receipt.target_section_id, original.receipt.target_book_id)),
            _key("old-master"),
            master_session=story.session,
        )
        .receipt
        == original.receipt
    )


@pytest.mark.parametrize("tamper", ["etag", "source", "fingerprint", "missing_receipt", "orphan"])
def test_caller_backup_rejects_semantic_corruption(
    content_engine: Engine, tmp_path: Path, tamper: str
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    first = _run(content_engine, agent, _move_command(page, _target(content_engine, page)))
    bundle = create_backup(
        content_engine,
        tmp_path / "caller-corruption",
        app_version="0.1.0a0",
        artifact_identity=BackupArtifactIdentity("synthetic/corruption", "sha256:" + "1" * 64),
    )
    database = bundle.bundle_path / BACKUP_FILENAME
    if tamper == "missing_receipt":
        _tamper(
            database,
            "trg_idempotency_records_no_delete",
            "DELETE FROM idempotency_records WHERE route_template LIKE '%/move'",
            (),
        )
    elif tamper == "orphan":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO auth_audit_events (id, library_id, actor_home_library_id, "
                "actor_caller_id, actor_credential_id, action, resource_type, resource_id, "
                "outcome, request_id, occurred_at) SELECT ?, library_id, actor_home_library_id, "
                "actor_caller_id, actor_credential_id, action, resource_type, resource_id, "
                "outcome, request_id, occurred_at FROM auth_audit_events "
                "WHERE action = 'content.page.move'",
                ("f" * 32,),
            )
    else:
        body = json.loads(first.response_body)
        parameters: tuple[bytes | str, ...]
        if tamper == "source":
            body["source_book_id"] = body["target_book_id"]
            statement = (
                "UPDATE idempotency_records SET response_body=? WHERE route_template LIKE '%/move'"
            )
            parameters = (json.dumps(body, separators=(",", ":")).encode(),)
        elif tamper == "fingerprint":
            statement = (
                "UPDATE idempotency_records SET request_fingerprint=zeroblob(32) "
                "WHERE route_template LIKE '%/move'"
            )
            parameters = ()
        else:
            statement = (
                "UPDATE idempotency_records SET response_etag=? WHERE route_template LIKE '%/move'"
            )
            parameters = ('"page-v2-' + "0" * 64 + '"',)
        # Only a closed synthetic backup copy is corrupted. Restore original SQL
        # so failure must be semantic rather than a schema hash mismatch.
        with closing(sqlite3.connect(database)) as connection, connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name="
                "'trg_idempotency_records_immutable_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_idempotency_records_immutable_update")
            connection.execute(statement, parameters)
            connection.execute(sql)
    with pytest.raises(BackupDatabaseError):
        validate_database(database)
