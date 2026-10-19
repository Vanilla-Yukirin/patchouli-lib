"""0030 changes behavior version only and refuses lossy downgrade."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from auth.test_optional_expiry_migration import _snapshot
from backup.conftest import _config
from content.conftest import OPERATION_TIME
from content.helpers import insert_page_graph, page_graph_values, seed_library_structure
from content.test_agent_page_lifecycle import _run
from content.test_agent_page_move import MovingAgent
from content.test_master_revision_restore_service import _etag, _page
from sqlalchemy import Engine, insert

from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import AuditOutcome, CallerKind, NewAuditEvent, NewCaller
from patchouli_lib.auth.service import CredentialIssuer
from patchouli_lib.backup import validate_database
from patchouli_lib.backup.errors import BackupDatabaseError
from patchouli_lib.backup.manifest import (
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    LIBRARY_SCOPES_SCHEMA_REVISION,
    OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION,
)
from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand
from patchouli_lib.content.schemas import PageRecord
from patchouli_lib.database import build_engine, immediate_transaction


def _historical_page_and_agent(
    engine: Engine, *, library_write: bool
) -> tuple[PageRecord, MovingAgent]:
    """Seed the pinned schema without current Library/Section/Book ORM reads."""
    library, section, book = seed_library_structure(engine)
    graph = page_graph_values(library_id=library, section_id=section, book_id=book)
    home, _, _ = seed_library_structure(engine, prefix="7", label="Agent home")
    with immediate_transaction(engine) as connection:
        insert_page_graph(connection, graph)
        auth = AuthRepository(connection)
        caller = auth.add_caller(
            NewCaller(
                id="a" * 32,
                library_id=home,
                kind=CallerKind.AGENT,
                name="Synthetic lifecycle Agent",
                created_at=OPERATION_TIME - 1,
                updated_at=OPERATION_TIME - 1,
            )
        )
        issued = CredentialIssuer(
            auth, clock=lambda: OPERATION_TIME - 1, id_factory=lambda: "7" * 32
        ).issue(caller, expires_at=OPERATION_TIME + 10_000_000)
        if library_write:
            identity = {
                "credential_id": issued.credential.id,
                "caller_id": caller.id,
                "home_library_id": home,
                "created_at": OPERATION_TIME,
            }
            connection.execute(
                insert(CredentialLibraryPolicy), {**identity, "mode": "library_grants"}
            )
            connection.execute(
                insert(CredentialLibraryGrant),
                {**identity, "target_library_id": library, "action": "write"},
            )
    return _page(engine, library, graph[0].page_id), MovingAgent(
        issued.value, home, caller.id, issued.credential.id
    )


@pytest.fixture(
    params=[LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION, OPTIONAL_CREDENTIAL_EXPIRY_SCHEMA_REVISION]
)
def lifecycle_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Engine]:
    path = tmp_path / "lifecycle.sqlite"
    command.upgrade(_config(path, monkeypatch), request.param)
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def test_behavior_upgrade_and_downgrade_preserve_exact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "behavior.sqlite"
    config = _config(path, monkeypatch)
    command.upgrade(config, "20261002_0029")
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            before = connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
            ).all()
        command.upgrade(config, LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                ).all()
                == before
            )
        command.downgrade(config, "20261002_0029")
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                ).all()
                == before
            )
    finally:
        engine.dispose()


def test_downgrade_rejects_library_lifecycle_success(
    lifecycle_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_engine = lifecycle_engine
    assert content_engine.url.database is not None
    path = Path(content_engine.url.database)
    config = _config(path, monkeypatch)
    with content_engine.connect() as connection:
        historical_revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    # Current authorization reads the 0032 scope columns. Build legitimate
    # history there, then restore the exact historical schema under test.
    # This tests historical downgrade safety without running new code on an
    # unmigrated database or weakening its production schema requirement.
    command.upgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    page, agent = _historical_page_and_agent(content_engine, library_write=True)
    _run(
        content_engine,
        agent,
        PageLifecycleCommand(
            library_id=page.library_id,
            page_id=page.page_id,
            action="delete",
            expected_etag=_etag(page),
            request_id="req_" + "d" * 32,
        ),
        "delete",
    )
    command.downgrade(config, historical_revision)
    validate_database(path, schema_revision=historical_revision)
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(config, "20261002_0029")
    # A completed 0031 -> 0030 prefix is committed before 0030 checks whether
    # lifecycle history permits continuing to 0029. No content may be lost.
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
        )
    assert _snapshot(path) == before
    validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)


def test_downgrade_rejects_orphan_new_audit(
    lifecycle_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_engine = lifecycle_engine
    page, agent = _historical_page_and_agent(content_engine, library_write=False)
    with immediate_transaction(content_engine) as connection:
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
    assert content_engine.url.database is not None
    path = Path(content_engine.url.database)
    before = _snapshot(path)
    with pytest.raises(RuntimeError, match="Cannot discard Library Page lifecycle"):
        command.downgrade(_config(path, monkeypatch), "20261002_0029")
    assert _snapshot(path) == before
    with content_engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
    # This fixture deliberately has an audit without its lifecycle receipt;
    # refusing downgrade must preserve that evidence, not make it valid.
    with pytest.raises(BackupDatabaseError):
        validate_database(path, schema_revision=LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION)
