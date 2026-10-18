"""Atomic Tag renaming/deletion keeps Page content and history intact."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

import pytest
from backup.conftest import APP_VERSION, seed_complete_database
from backup.test_service import _create
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from pydantic import ValidationError
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.contracts import (
    MasterPageTagFormInput,
    MasterTagBatchChange,
    MasterTagBatchInput,
    MasterTagFormInput,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.admin.session import AdminSessionCodec, MasterAdminSession
from patchouli_lib.admin.tag_management import TagBatchConflictError
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.backup import restore_backup, validate_database, verify_backup_bundle
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.search.index_v2 import rebuild_search_index
from patchouli_lib.search.query_v2 import SearchQueryV2Wire
from patchouli_lib.search.service_v2 import SearchScopeError, search_pages_for_master
from patchouli_lib.tags.models import PageTag, Tag
from patchouli_lib.tags.repository import TagRepository
from patchouli_lib.tags.service import TagNotFoundError, TagValidationError

Context = tuple[AdminActionService, Engine, MasterAdminSession]
FIRST = "1" * 32
SECOND = "4" * 32
TAG = "a" * 32
OTHER = "b" * 32


@pytest.fixture
def tag_context() -> Iterator[Context]:
    engine = build_engine("sqlite:///:memory:")
    Tag.metadata.create_all(engine)
    for prefix in ("1", "4"):
        library_id, section_id, book_id = seed_library_structure(
            engine, prefix=prefix, label=prefix
        )
        with immediate_transaction(engine) as connection:
            repository = TagRepository(connection)
            repository.add_tag(
                library_id=library_id, tag_id=TAG, name="Original", created_at=1_000_000
            )
            repository.add_tag(
                library_id=library_id, tag_id=OTHER, name="Other", created_at=1_000_000
            )
            page_graph = page_graph_values(
                library_id=library_id, section_id=section_id, book_id=book_id
            )
            insert_page_graph(connection, page_graph)
            repository.attach_page(
                library_id=library_id,
                page_uid=page_graph[0].page_uid,
                tag_id=TAG,
                created_at=2_000_000,
            )
    with immediate_transaction(engine) as connection:
        MasterTokenRepository(
            connection, identity_factory=lambda: "c" * 32
        ).initialize_from_local_cli("synthetic master token material 0001", now=1_000_000)
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic_csrf_token",
        identity_id="c" * 32,
        session_generation=1,
    )
    try:
        yield AdminActionService(engine, clock=lambda: 3_000_000), engine, session
    finally:
        engine.dispose()


def _change(
    library_id: str = FIRST,
    tag_id: str = TAG,
    expected_name: str = "Original",
    new_name: str | None = None,
) -> MasterTagBatchChange:
    return MasterTagBatchChange(
        library_id=library_id, tag_id=tag_id, expected_name=expected_name, new_name=new_name
    )


def _rename(*changes: MasterTagBatchChange) -> MasterTagBatchInput:
    return MasterTagBatchInput(operation="rename", changes=changes)


def _names(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                select(Tag.library_id, Tag.id, Tag.display_name).order_by(Tag.library_id, Tag.id)
            )
        ]


def test_cross_library_batch_rename_preserves_associations(tag_context: Context) -> None:
    service, engine, session = tag_context
    request = _rename(_change(new_name="CAFÉ"), _change(SECOND, new_name="CAFÉ"))
    assert service.manage_tags_as_master(request, master_session=session) == 2
    with engine.connect() as connection:
        assert connection.scalars(select(Tag.match_key).where(Tag.id == TAG)).all() == [
            "café",
            "café",
        ]
        assert connection.scalar(select(func.count()).select_from(PageTag)) == 2
        assert connection.scalars(select(MasterAuditEvent.action)).all() == [
            "tag.rename",
            "tag.rename",
        ]
    assert (
        service.manage_tags_as_master(
            _rename(_change(expected_name="CAFÉ", new_name="CAFÉ")), master_session=session
        )
        == 0
    )


@pytest.mark.parametrize("problem", ["stale", "missing", "collision", "swap", "invalid"])
def test_batch_preflight_failure_never_applies_first_change(
    tag_context: Context, problem: str
) -> None:
    service, engine, session = tag_context
    before = _names(engine)
    second = _change(SECOND, new_name="Renamed")
    exception: type[Exception] = TagBatchConflictError
    if problem == "stale":
        second = _change(SECOND, expected_name="Stale", new_name="Renamed")
    elif problem == "missing":
        second = _change(SECOND, tag_id="f" * 32, new_name="Renamed")
        exception = TagNotFoundError
    elif problem == "collision":
        second = _change(SECOND, new_name="oThEr")
    elif problem == "invalid":
        second = _change(SECOND, new_name=" bad\nname ")
        exception = TagValidationError
    changes = (_change(new_name="Renamed"), second)
    if problem == "swap":
        changes = (
            _change(new_name="Other"),
            _change(tag_id=OTHER, expected_name="Other", new_name="Original"),
        )
    with pytest.raises(exception):
        service.manage_tags_as_master(_rename(*changes), master_session=session)
    assert _names(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 0


def test_batch_rejects_duplicate_destination_names(tag_context: Context) -> None:
    service, _, session = tag_context
    with pytest.raises(TagBatchConflictError):
        service.manage_tags_as_master(
            _rename(
                _change(new_name="Same"),
                _change(tag_id=OTHER, expected_name="Other", new_name="SAME"),
            ),
            master_session=session,
        )


def test_delete_removes_only_selected_tags_and_associations(tag_context: Context) -> None:
    service, engine, session = tag_context
    with engine.connect() as connection:
        before_pages = connection.execute(select(Page.__table__)).all()
        before_revisions = connection.execute(select(Revision.__table__)).all()
    assert (
        service.manage_tags_as_master(
            MasterTagBatchInput(operation="delete", changes=(_change(),), confirm_delete=True),
            master_session=session,
        )
        == 1
    )
    with engine.connect() as connection:
        assert connection.execute(select(Page.__table__)).all() == before_pages
        assert connection.execute(select(Revision.__table__)).all() == before_revisions
        assert connection.scalars(select(PageTag.library_id)).all() == [SECOND]
        assert connection.scalar(select(func.count()).select_from(Tag)) == 3
        assert connection.scalar(select(MasterAuditEvent.action)) == "tag.delete"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_expired_master_session_cannot_manage_tags(tag_context: Context) -> None:
    service, engine, session = tag_context
    before = _names(engine)
    with pytest.raises(AuthenticationError):
        service.manage_tags_as_master(
            _rename(_change(new_name="Renamed")), master_session=replace(session, expires_at=0)
        )
    assert _names(engine) == before


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_audit_failure_rolls_back_whole_batch(
    tag_context: Context, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service, engine, session = tag_context
    before = _names(engine)
    original = MasterAuditRepository.add_success
    count = 0

    def fail_second(self: MasterAuditRepository, **kwargs: object) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Synthetic audit failure")
        original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(MasterAuditRepository, "add_success", fail_second)
    request = (
        _rename(_change(new_name="Renamed"), _change(SECOND, new_name="Renamed"))
        if operation == "rename"
        else MasterTagBatchInput(
            operation="delete", changes=(_change(), _change(SECOND)), confirm_delete=True
        )
    )
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        service.manage_tags_as_master(request, master_session=session)
    assert _names(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(PageTag)) == 2
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 0


@pytest.mark.parametrize("invalid", ["unconfirmed", "empty", "duplicate", "missing_name", "excess"])
def test_batch_contract_rejects_invalid_requests(invalid: str) -> None:
    with pytest.raises(ValidationError):
        if invalid == "unconfirmed":
            MasterTagBatchInput(operation="delete", changes=(_change(),))
        elif invalid == "empty":
            _rename()
        elif invalid == "duplicate":
            _rename(_change(new_name="A"), _change(new_name="B"))
        elif invalid == "missing_name":
            _rename(_change())
        else:
            _rename(*(_change(tag_id=f"{i:032x}", new_name=f"Name {i}") for i in range(201)))


def test_real_migration_tag_lifecycle_search_and_verified_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="patchouli-tag-batch-test-") as temporary:
        root = Path(temporary)
        engine = seed_complete_database(root / "source.sqlite", monkeypatch)
        try:
            with immediate_transaction(engine) as connection:
                MasterTokenRepository(
                    connection, identity_factory=lambda: "f" * 32
                ).initialize_from_local_cli("synthetic master token material 0001", now=1_000_000)
            codec = AdminSessionCodec(b"synthetic master session secret 0001", ttl_seconds=600)
            encoded, session = codec.issue_master("f" * 32, 1)
            service = AdminActionService(engine, clock=lambda: 3_000_000)
            with engine.connect() as connection:
                page = connection.execute(select(Page.__table__)).mappings().one()
                before_pages = connection.execute(select(Page.__table__)).all()
                before_revisions = connection.execute(select(Revision.__table__)).all()
                before_audit = connection.exec_driver_sql("SELECT * FROM auth_audit_events").all()
                before_receipts = connection.exec_driver_sql(
                    "SELECT * FROM idempotency_records"
                ).all()
            tag_id, created = service.create_tag_as_master(
                FIRST, MasterTagFormInput(name="Original"), master_session=session
            )
            assert created
            service.set_page_tag_as_master(
                FIRST,
                page.section_id,
                page.book_id,
                page.page_id,
                MasterPageTagFormInput(tag_id=tag_id, operation="attach"),
                master_session=session,
            )
            rebuild_search_index(engine)
            tag_query = SearchQueryV2Wire.model_validate(
                {"tags_any": [{"library_id": FIRST, "tag_id": tag_id}]}
            ).to_query()
            assert len(search_pages_for_master(engine, encoded, codec, tag_query).items) == 1
            service.manage_tags_as_master(
                _rename(_change(tag_id=tag_id, new_name="Renamed")), master_session=session
            )
            assert len(search_pages_for_master(engine, encoded, codec, tag_query).items) == 1
            renamed_bundle = _create(engine, root / "renamed-bundle")
            verify_backup_bundle(renamed_bundle.bundle_path, app_version=APP_VERSION)
            service.manage_tags_as_master(
                MasterTagBatchInput(
                    operation="delete",
                    changes=(_change(tag_id=tag_id, expected_name="Renamed"),),
                    confirm_delete=True,
                ),
                master_session=session,
            )
            with pytest.raises(SearchScopeError):
                search_pages_for_master(engine, encoded, codec, tag_query)
            text_query = SearchQueryV2Wire.model_validate({"keywords": ["Synthetic"]}).to_query()
            assert len(search_pages_for_master(engine, encoded, codec, text_query).items) == 1
            with engine.connect() as connection:
                assert connection.execute(select(Page.__table__)).all() == before_pages
                assert connection.execute(select(Revision.__table__)).all() == before_revisions
                assert (
                    connection.exec_driver_sql("SELECT * FROM auth_audit_events").all()
                    == before_audit
                )
                assert (
                    connection.exec_driver_sql("SELECT * FROM idempotency_records").all()
                    == before_receipts
                )
                assert (
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM search_dirty_pages"
                    ).scalar_one()
                    == 0
                )
                assert connection.scalars(select(MasterAuditEvent.action)).all() == [
                    "tag.create",
                    "tag.page.attach",
                    "tag.rename",
                    "tag.delete",
                ]
            bundle = _create(engine, root / "deleted-bundle")
            verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION)
            restored = root / "restored.sqlite"
            restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
            validate_database(restored)
            with closing(sqlite3.connect(restored)) as connection:
                assert connection.execute("SELECT count(*) FROM tags").fetchone() == (0,)
                assert connection.execute("SELECT count(*) FROM page_tags").fetchone() == (0,)
                assert connection.execute("SELECT count(*) FROM pages").fetchone() == (1,)
                assert connection.execute("SELECT count(*) FROM revisions").fetchone() == (1,)
                assert connection.execute(
                    "SELECT count(*) FROM admin_master_audit_events"
                ).fetchone() == (4,)
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            engine.dispose()
