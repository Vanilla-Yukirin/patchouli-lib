"""Library-write lifecycle retains complete snapshots and actual actor home."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, delete

from patchouli_lib.admin.pages import dashboard_page
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.auth.models import CredentialLibraryGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, NewAuditEvent
from patchouli_lib.auth.service import AuthorizationError
from patchouli_lib.content.file_set_service import FileSetPreconditionFailedError
from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand, lifecycle_key_digest
from patchouli_lib.content.page_lifecycle_service import (
    CallerPageLifecycleService,
    PageLifecycleStateConflictError,
)
from patchouli_lib.content.page_move_core import PageMoveNotFoundError
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, ReplayResponse
from patchouli_lib.idempotency.service import IdempotencyConflictError

from .conftest import OPERATION_TIME
from .page_move_helpers import _target
from .test_agent_page_move import MovingAgent, _agent, _move_command
from .test_agent_page_move import _run as _run_move
from .test_master_revision_restore_service import _etag, _key, _page, _story


def _orphan_audit(engine: Engine) -> None:
    story = _story(engine)
    page = _page(engine, story.command.library_id, story.command.page_id)
    agent = _agent(engine, page.library_id)
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_audit_event(
            NewAuditEvent(
                id="f" * 32,
                library_id=page.library_id,
                actor_home_library_id=agent.home,
                actor_caller_id=agent.caller_id,
                actor_credential_id=agent.credential_id,
                action="content.page.delete",
                resource_type="page",
                resource_id=page.page_id,
                outcome=AuditOutcome.SUCCEEDED,
                request_id="req_" + "f" * 32,
                occurred_at=OPERATION_TIME + 40,
            )
        )


def _run(
    engine: Engine, agent: MovingAgent, command: PageLifecycleCommand, key: str
) -> OriginalResponse | ReplayResponse:
    with immediate_transaction(engine) as connection:
        return CallerPageLifecycleService(connection, clock=lambda: OPERATION_TIME + 40).transition(
            agent.token, command, _key(key)
        )


@pytest.mark.parametrize("kind", ["legacy", "markdown", "mixed", "binary"])
def test_foreign_library_delete_restore_and_original_replay(
    content_engine: Engine, kind: str
) -> None:
    story = _story(content_engine, kind)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    with content_engine.connect() as connection:
        snapshots = connection.exec_driver_sql("SELECT * FROM revisions").all()
        files = connection.exec_driver_sql("SELECT * FROM revision_files").all()
    command = PageLifecycleCommand(
        library_id=page.library_id,
        page_id=page.page_id,
        action="delete",
        expected_etag=_etag(page),
        request_id="req_" + "d" * 32,
    )
    removed = _run(content_engine, agent, command, "delete")
    trashed = _page(content_engine, page.library_id, page.page_id)
    assert trashed.deleted_at is not None
    assert trashed.current_revision_id == page.current_revision_id
    assert json.loads(removed.response_body)["state"] == "trashed"
    deleted_activity = AdminReadModel(content_engine).recent_content_activity(
        actor=(agent.home, agent.caller_id)
    )
    deleted_html = dashboard_page("synthetic-csrf", activities=deleted_activity)
    assert deleted_html.count("Deleted a page") == 1
    assert f"/trash/{page.page_id}" in deleted_html
    restore = command.model_copy(update={"action": "restore", "expected_etag": _etag(trashed)})
    _run(content_engine, agent, restore, "restore")
    assert _page(content_engine, page.library_id, page.page_id).deleted_at is None
    current = _page(content_engine, page.library_id, page.page_id)
    _run_move(content_engine, agent, _move_command(current, _target(content_engine, current)))
    replay = _run(content_engine, agent, command, "delete")
    assert isinstance(replay, ReplayResponse)
    assert (replay.response_body, replay.response_etag) == (
        removed.response_body,
        removed.response_etag,
    )
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT * FROM revisions").all() == snapshots
        assert connection.exec_driver_sql("SELECT * FROM revision_files").all() == files
        rows = connection.exec_driver_sql(
            "SELECT library_id, actor_home_library_id, actor_caller_id, actor_credential_id "
            "FROM auth_audit_events WHERE action IN ('content.page.delete','content.page.restore')"
        ).all()
        assert [tuple(row) for row in rows] == [
            (page.library_id, agent.home, agent.caller_id, agent.credential_id)
        ] * 2
    items = AdminReadModel(content_engine).recent_content_activity(
        actor=(agent.home, agent.caller_id)
    )
    assert len(items) == 3
    assert {item.action for item in items} == {
        "content.page.delete",
        "content.page.restore",
        "content.page.move",
    }
    html = dashboard_page("synthetic-csrf", activities=items)
    assert html.count("Deleted a page") == html.count("Restored a page") == 1
    with immediate_transaction(content_engine) as connection:
        connection.execute(delete(CredentialLibraryGrant))
    with pytest.raises(AuthorizationError):
        _run(content_engine, agent, command, "delete")


def test_key_digest_is_home_bound_without_illegal_duplicate_callers() -> None:
    assert lifecycle_key_digest(b"x" * 32, "1" * 32) != lifecycle_key_digest(b"x" * 32, "2" * 32)


def test_conflicts_never_create_an_extra_transition(content_engine: Engine) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    agent = _agent(content_engine, page.library_id)
    command = PageLifecycleCommand(
        library_id=page.library_id,
        page_id=page.page_id,
        action="delete",
        expected_etag=_etag(page),
        request_id="req_" + "d" * 32,
    )
    with pytest.raises(PageLifecycleStateConflictError):
        _run(content_engine, agent, command.model_copy(update={"action": "restore"}), "restore")
    with pytest.raises(PageMoveNotFoundError):
        _run(
            content_engine,
            agent,
            command.model_copy(update={"page_id": page.page_id + "missing"}),
            "missing",
        )
    first = _run(content_engine, agent, command, "delete")
    trashed = _page(content_engine, page.library_id, page.page_id)
    with pytest.raises(FileSetPreconditionFailedError):
        _run(content_engine, agent, command, "stale")
    with pytest.raises(PageLifecycleStateConflictError):
        _run(
            content_engine,
            agent,
            command.model_copy(update={"expected_etag": first.response_etag}),
            "same-state",
        )
    with pytest.raises(IdempotencyConflictError):
        _run(
            content_engine,
            agent,
            command.model_copy(update={"expected_etag": first.response_etag}),
            "delete",
        )
    assert _page(content_engine, page.library_id, page.page_id) == trashed
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM page_lifecycle_events").scalar_one()
            == 1
        )
