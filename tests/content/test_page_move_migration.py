"""Direct SQL cannot forge, replace, bundle or lose Page movement history."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from backup.conftest import _config
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from content.page_move_helpers import _command, _target
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.page_move_service import MasterPageMoveService
from patchouli_lib.content.page_move_models import PageMoveEvent, PageMoveGuard
from patchouli_lib.database import immediate_transaction

from .conftest import OPERATION_TIME
from .test_master_revision_restore_service import _key, _page, _story


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE pages SET book_id = :book, section_id = :section, updated_at = updated_at + 1",
        "UPDATE pages SET book_id = :book, section_id = :section, title = 'bundled'",
    ],
)
def test_direct_path_changes_require_exact_guard(content_engine: Engine, statement: str) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    section, book = _target(content_engine, page)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.exec_driver_sql(statement, {"section": section, "book": book})
    assert _page(content_engine, page.library_id, page.page_id) == page


def _guard(engine: Engine) -> dict[str, object]:
    story = _story(engine)
    page = _page(engine, story.command.library_id, story.command.page_id)
    section, book = _target(engine, page)
    with immediate_transaction(engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=story.session.identity_id,
            session_generation=story.session.session_generation,
            session_fingerprint=story.session.audit_fingerprint(),
            action="content.page.move",
            target_type="page",
            target_id=f"{page.library_id}:{page.page_uid.hex()}",
            occurred_at=page.updated_at + 1,
            event_id="a" * 32,
        )
    return {
        "library_id": page.library_id,
        "page_uid": page.page_uid,
        "sequence": 1,
        "old_section_id": page.section_id,
        "old_book_id": page.book_id,
        "new_section_id": section,
        "new_book_id": book,
        "old_updated_at": page.updated_at,
        "changed_at": page.updated_at + 1,
        "at_revision_id": page.current_revision_id,
        "at_revision_number": page.current_revision_number,
        "occurred_at_at_event": page.occurred_at,
        "master_audit_event_id": "a" * 32,
    }


def test_pending_guard_cannot_commit_or_be_deleted(content_engine: Engine) -> None:
    values = _guard(content_engine)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveGuard), values)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveGuard), values)
        connection.exec_driver_sql("DELETE FROM page_move_guards")
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM page_move_guards").scalar_one() == 0


def test_direct_event_insert_without_transition_is_rejected(content_engine: Engine) -> None:
    values = _guard(content_engine)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveEvent), values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", 2),
        ("old_updated_at", 1),
        ("changed_at", OPERATION_TIME + 100),
        ("at_revision_number", 1),
        ("occurred_at_at_event", 1),
        ("master_audit_event_id", "b" * 32),
        ("new_book_id", "0" * 32),
    ],
)
def test_guard_rejects_wrong_prestate_audit_sequence_and_destination(
    content_engine: Engine,
    field: str,
    value: object,
) -> None:
    values = _guard(content_engine)
    values[field] = value
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveGuard), values)


def test_guard_cannot_be_replaced_or_updated(content_engine: Engine) -> None:
    values = _guard(content_engine)
    for statement in (
        "INSERT OR REPLACE INTO page_move_guards SELECT * FROM page_move_guards",
        "UPDATE page_move_guards SET changed_at = changed_at + 1",
    ):
        with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
            connection.execute(insert(PageMoveGuard), values)
            connection.exec_driver_sql(statement)


@pytest.mark.parametrize(
    "bundle",
    [
        "title = 'bundled'",
        "occurred_at = occurred_at + 1",
        "deleted_at = updated_at + 1",
        "page_type = 'other'",
    ],
)
def test_guard_rejects_other_mutations_in_same_statement(
    content_engine: Engine, bundle: str
) -> None:
    values = _guard(content_engine)
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.execute(insert(PageMoveGuard), values)
        connection.exec_driver_sql(
            "UPDATE pages SET section_id = ?, book_id = ?, updated_at = ?, " + bundle,
            (values["new_section_id"], values["new_book_id"], values["changed_at"]),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE page_move_events SET changed_at = changed_at + 1",
        "DELETE FROM page_move_events",
        "INSERT OR REPLACE INTO page_move_events SELECT * FROM page_move_events",
        "UPDATE admin_master_move_receipts SET operation_at = operation_at + 1",
        "DELETE FROM admin_master_move_receipts",
        "INSERT OR REPLACE INTO admin_master_move_receipts SELECT * "
        "FROM admin_master_move_receipts",
        "INSERT OR REPLACE INTO admin_master_move_receipts "
        "SELECT identity_id, operation, zeroblob(32), "
        "request_fingerprint, library_id, page_uid, page_id, "
        "source_section_id, source_book_id, target_section_id, "
        "target_book_id, revision_id, revision_number, "
        "original_occurred_at, original_page_updated_at, "
        "result_updated_at, request_etag, response_etag, "
        "operation_at, changed, move_sequence, master_audit_event_id "
        "FROM admin_master_move_receipts",
    ],
)
def test_event_and_receipt_are_immutable_and_single_consumption(
    content_engine: Engine, statement: str
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME).move_page(
        _command(page, _target(content_engine, page)),
        _key("move"),
        master_session=story.session,
    )
    with pytest.raises(IntegrityError), immediate_transaction(content_engine) as connection:
        connection.exec_driver_sql(statement)


@pytest.mark.parametrize("changed", [False, True])
def test_downgrade_refuses_real_and_noop_success(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, page) if changed else (page.section_id, page.book_id)
    MasterPageMoveService(content_engine, clock=lambda: OPERATION_TIME).move_page(
        _command(page, target),
        _key("move"),
        master_session=story.session,
    )
    assert content_engine.url.database is not None
    with pytest.raises(RuntimeError, match="Cannot discard"):
        command.downgrade(_config(Path(content_engine.url.database), monkeypatch), "20261001_0027")
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "20261001_0028"
        )


def test_clean_downgrade_restores_exact_0027_schema(
    content_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from patchouli_lib.backup.validation import validate_database

    assert content_engine.url.database is not None
    path = Path(content_engine.url.database)
    command.downgrade(_config(path, monkeypatch), "20261001_0027")
    assert validate_database(path, schema_revision="20261001_0027")
    command.upgrade(_config(path, monkeypatch), "head")
    assert validate_database(path)
