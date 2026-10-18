"""Master file-set changes appear once in the existing content timeline."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from time import time

import pytest
from alembic import command as alembic
from alembic.config import Config
from sqlalchemy import Engine, insert, update

from patchouli_lib.admin.contracts import MasterDeletePageFormInput, MasterUpdatePageTitleInput
from patchouli_lib.admin.file_set_receipts import MasterFileSetReceiptRow
from patchouli_lib.admin.file_set_service import MasterFileSetService
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.pages import dashboard_page
from patchouli_lib.admin.read_model import AdminReadModel
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.models import AuditEvent, Caller, Credential
from patchouli_lib.content.file_set_create_service import FileSetCreateCommand
from patchouli_lib.content.file_set_write_service import FileSetAppendCommand
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.content.schemas import ArchiveIdempotencyKey, ArchiveSourceInput
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import parse_occurrence_time
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.tags.models import Tag

_AT = parse_occurrence_time("2026-08-13T10:00:01.000000Z").utc_microseconds
_FILES_1 = (("content.md", b"# One\n"), ("payload.bin", b"\x00\xff"))
_FILES_2 = (("content.md", b"# Two\n"),)
_FILES_3 = (("content.md", b"# Three\n"),)


@pytest.fixture
def activity_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'activity.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    alembic.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _key(value: str) -> ArchiveIdempotencyKey:
    return ArchiveIdempotencyKey(key_digest=digest_idempotency_key(value))


def _setup(engine: Engine) -> tuple[FileSetCreateCommand, MasterAdminSession]:
    ids = iter(("1" * 32, "2" * 32, "3" * 32))
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection), id_factory=lambda: next(ids), clock=lambda: 1_000_000
        ).seed(
            LibraryStructureSeed(
                library_name="Synthetic Library",
                section_name="Synthetic Section",
                book_name="Synthetic Book",
            )
        )
        master = MasterTokenRepository(connection).initialize_from_local_cli(
            "synthetic activity master token material", now=_AT
        )
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic-csrf",
        identity_id=master.identity_id,
        session_generation=master.session_generation,
    )
    create = FileSetCreateCommand(
        library_id=structure.library.id,
        section_id=structure.section.id,
        book_id=structure.book.id,
        title="Original title",
        occurred_at=_AT - 10,
        files=_FILES_1,
        source=ArchiveSourceInput(kind="synthetic"),
        request_id="req_" + "1" * 32,
    )
    return create, session


def _append(
    create: FileSetCreateCommand,
    page_id: str,
    etag: str,
    files: tuple[tuple[str, bytes], ...],
) -> FileSetAppendCommand:
    return FileSetAppendCommand(
        library_id=create.library_id,
        section_id=create.section_id,
        page_id=page_id,
        expected_etag=etag,
        files=files,
        source=create.source,
        request_id="req_" + "2" * 32,
    )


def _page_path(create: FileSetCreateCommand, page_id: str) -> str:
    return (
        f"/admin/libraries/{create.library_id}/sections/{create.section_id}"
        f"/books/{create.book_id}/pages/{page_id}"
    )


def _current_etag(engine: Engine, library_id: str, page_id: str) -> str:
    with engine.connect() as connection:
        page = ContentRepository(connection).get_page(library_id, page_id)
        assert page is not None
        return page_current_etag(
            page.page_uid,
            page.current_revision_id,
            page.current_revision_number,
            page.occurred_at,
            page.updated_at,
        )


def test_master_changes_link_exact_revisions_without_noop_or_replay(
    activity_engine: Engine,
) -> None:
    create, session = _setup(activity_engine)
    service = MasterFileSetService(activity_engine, clock=lambda: _AT)
    first = service.create_page(create, _key("create"), master_session=session)
    second_command = _append(create, first.receipt.page_id, first.receipt.response_etag, _FILES_2)
    second = service.revise_page(
        second_command, create.book_id, _key("second"), master_session=session
    )
    unchanged_command = _append(
        create, first.receipt.page_id, second.receipt.response_etag, _FILES_2
    )
    unchanged = service.revise_page(
        unchanged_command, create.book_id, _key("unchanged"), master_session=session
    )
    assert unchanged.receipt.changed == 0
    assert service.create_page(create, _key("create"), master_session=session).replayed
    assert service.revise_page(
        second_command, create.book_id, _key("second"), master_session=session
    ).replayed
    third = service.revise_page(
        _append(create, first.receipt.page_id, second.receipt.response_etag, _FILES_3),
        create.book_id,
        _key("third"),
        master_session=session,
    )

    activity = AdminReadModel(activity_engine).recent_content_activity()
    assert len(activity) == 3
    assert {(item.action, item.revision_number) for item in activity} == {
        ("content.page.file_set.create", 1),
        ("content.page.file_set.revise", 2),
        ("content.page.file_set.revise", 3),
    }
    assert all(item.actor_name == "Administrator" and item.actor_id is None for item in activity)
    assert third.receipt.revision_number == 3
    html = dashboard_page("synthetic-csrf", activities=activity)
    path = _page_path(create, first.receipt.page_id)
    for revision_number in (1, 2, 3):
        assert f'href="{path}/revisions/{revision_number}"' in html
    assert html.count("Created a page") == 1
    assert html.count("Revised a page") == 2


def test_master_activity_uses_current_title_and_trash_state(activity_engine: Engine) -> None:
    create, session = _setup(activity_engine)
    service = MasterFileSetService(activity_engine, clock=lambda: _AT)
    first = service.create_page(create, _key("create"), master_session=session)
    second = service.revise_page(
        _append(create, first.receipt.page_id, first.receipt.response_etag, _FILES_2),
        create.book_id,
        _key("second"),
        master_session=session,
    )
    renamed = "Renamed <synthetic>"
    actions = AdminActionService(activity_engine, clock=lambda: _AT + 20)
    actions.update_page_title_as_master(
        create.library_id,
        create.section_id,
        create.book_id,
        first.receipt.page_id,
        MasterUpdatePageTitleInput(
            title=renamed, expected_updated_at=second.receipt.original_page_updated_at
        ),
        master_session=session,
    )
    activity = [
        item
        for item in AdminReadModel(activity_engine).recent_content_activity()
        if item.action.startswith("content.page.file_set.")
    ]
    assert len(activity) == 2
    assert all(item.page_title == renamed and not item.page_deleted for item in activity)
    path = _page_path(create, first.receipt.page_id)
    html = dashboard_page("synthetic-csrf", activities=tuple(activity))
    assert "Renamed &lt;synthetic&gt;" in html
    assert f'href="{path}/revisions/1"' in html
    assert f'href="{path}/revisions/2"' in html

    actions.delete_page_as_master(
        create.library_id,
        create.section_id,
        create.book_id,
        first.receipt.page_id,
        MasterDeletePageFormInput(
            expected_etag=_current_etag(activity_engine, create.library_id, first.receipt.page_id),
            confirm_delete="yes",
        ),
        master_session=session,
    )
    deleted = [
        item
        for item in AdminReadModel(activity_engine).recent_content_activity()
        if item.action.startswith("content.page.file_set.")
    ]
    assert len(deleted) == 2
    assert all(item.page_title == renamed and item.page_deleted for item in deleted)
    html = dashboard_page("synthetic-csrf", activities=tuple(deleted))
    trash = (
        f"/admin/libraries/{create.library_id}/sections/{create.section_id}"
        f"/trash/{first.receipt.page_id}"
    )
    assert html.count(f'href="{trash}"') == 2
    assert f'href="{path}/revisions/' not in html


def _seed_agent(engine: Engine, library_id: str) -> tuple[str, str]:
    caller_id, credential_id = "a" * 32, "b" * 32
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(Caller),
            {
                "id": caller_id,
                "library_id": library_id,
                "kind": "agent",
                "name": "Synthetic Agent",
                "description": "Synthetic activity actor",
                "policy_version": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        connection.execute(
            insert(Credential),
            {
                "id": credential_id,
                "library_id": library_id,
                "caller_id": caller_id,
                "selector": "a" * 22,
                "token_version": 1,
                "verifier": b"v" * 32,
                "expires_at": _AT + 1_000_000,
                "created_at": 1,
                "updated_at": 1,
            },
        )
    return caller_id, credential_id


def test_master_and_agent_same_timestamp_cross_page_boundary(activity_engine: Engine) -> None:
    create, session = _setup(activity_engine)
    result = MasterFileSetService(activity_engine, clock=lambda: _AT).create_page(
        create, _key("create"), master_session=session
    )
    audit_id = result.receipt.master_audit_event_id
    assert audit_id is not None
    caller_id, credential_id = _seed_agent(activity_engine, create.library_id)
    newer_ids = [f"{index:032x}" for index in range(1, 21) if f"{index:032x}" != audit_id][:19]
    assert len(newer_ids) == 19
    with immediate_transaction(activity_engine) as connection:
        connection.execute(
            insert(Tag),
            [
                {
                    "library_id": create.library_id,
                    "id": f"{index:032x}",
                    "display_name": f"Synthetic tag {index}",
                    "match_key": f"synthetic tag {index}",
                    "created_at": 1,
                }
                for index in range(1, 21)
            ],
        )
        connection.execute(
            insert(AuditEvent),
            [
                {
                    "id": event_id,
                    "library_id": create.library_id,
                    "actor_home_library_id": create.library_id,
                    "actor_caller_id": caller_id,
                    "actor_credential_id": credential_id,
                    "action": "tag.create",
                    "resource_type": "tag",
                    "resource_id": f"{index:032x}",
                    "outcome": "succeeded",
                    "request_id": "synthetic-pagination-request",
                    "occurred_at": _AT + 1,
                }
                for index, event_id in enumerate(newer_ids, start=1)
            ]
            + [
                {
                    "id": audit_id,
                    "library_id": create.library_id,
                    "actor_home_library_id": create.library_id,
                    "actor_caller_id": caller_id,
                    "actor_credential_id": credential_id,
                    "action": "tag.create",
                    "resource_type": "tag",
                    "resource_id": f"{20:032x}",
                    "outcome": "succeeded",
                    "request_id": "synthetic-pagination-request",
                    "occurred_at": _AT,
                }
            ],
        )

    model = AdminReadModel(activity_engine)
    first = model.content_activity_page()
    assert len(first.items) == 20
    assert first.items[-1].action == "content.page.file_set.create"
    assert first.next_cursor == f"{_AT}:m:{audit_id}"
    second = model.content_activity_page(before=first.next_cursor)
    assert len(second.items) == 1
    assert second.items[0].tag_name == "Synthetic tag 20"
    assert second.next_cursor is None
    actor_page = model.content_activity_page(actor=(create.library_id, caller_id))
    assert len(actor_page.items) == 20
    assert all(item.action == "tag.create" for item in actor_page.items)
    assert all(item.actor_id == caller_id for item in actor_page.items)


@pytest.mark.parametrize("damage", ["missing", "mismatched_time"])
def test_master_activity_fails_closed_on_bad_receipt_link(
    activity_engine: Engine, damage: str
) -> None:
    create, session = _setup(activity_engine)
    result = MasterFileSetService(activity_engine, clock=lambda: _AT).create_page(
        create, _key("create"), master_session=session
    )
    with immediate_transaction(activity_engine) as connection:
        if damage == "missing":
            MasterAuditRepository(connection).add_success(
                identity_id=session.identity_id,
                session_generation=session.session_generation,
                session_fingerprint=session.audit_fingerprint(),
                action="content.page.file_set.revise",
                target_type="page",
                target_id=f"{create.library_id}:{result.receipt.page_uid.hex()}",
                occurred_at=_AT + 1,
                event_id="e" * 32,
            )
        else:
            connection.exec_driver_sql("DROP TRIGGER trg_master_file_set_receipts_no_update")
            connection.execute(
                update(MasterFileSetReceiptRow)
                .where(
                    MasterFileSetReceiptRow.master_audit_event_id
                    == result.receipt.master_audit_event_id
                )
                .values(operation_at=_AT + 1)
            )
    with pytest.raises(RuntimeError):
        AdminReadModel(activity_engine).content_activity_page()
