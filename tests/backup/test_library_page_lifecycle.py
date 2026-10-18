"""Portable lifecycle snapshots preserve exact history and reject misbinding."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from content.conftest import content_engine as content_engine
from content.page_move_helpers import _command, _target
from content.test_agent_page_lifecycle import _orphan_audit, _run
from content.test_agent_page_move import _agent, _move_command
from content.test_agent_page_move import _run as _run_move
from content.test_master_revision_restore_service import _etag, _key, _page, _story
from sqlalchemy import Engine

from patchouli_lib.admin.page_move_service import MasterPageMoveService
from patchouli_lib.backup import BackupDatabaseError, restore_backup, validate_database
from patchouli_lib.content.page_lifecycle_schemas import PageLifecycleCommand
from patchouli_lib.database import CURRENT_SCHEMA_REVISION

from .conftest import APP_VERSION, _config
from .test_master_occurrence_backup import _historical_bundle, _tamper
from .test_service import _create


def _deleted(engine: Engine) -> None:
    story = _story(engine)
    page = _page(engine, story.command.library_id, story.command.page_id)
    agent = _agent(engine, page.library_id)
    _run(
        engine,
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


def test_backup_restore_preserves_new_lifecycle_and_file_bytes(
    content_engine: Engine, tmp_path: Path
) -> None:
    _deleted(content_engine)
    tables = (
        "pages",
        "revisions",
        "revision_files",
        "page_lifecycle_events",
        "auth_audit_events",
        "idempotency_records",
    )
    with content_engine.connect() as connection:
        expected = {
            table: [tuple(row) for row in connection.exec_driver_sql(f"SELECT * FROM {table}")]
            for table in tables
        }
    bundle = _create(content_engine, tmp_path / "bundle")
    assert bundle.manifest.schema_revision == CURRENT_SCHEMA_REVISION
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "restored.sqlite", app_version=APP_VERSION
    )
    assert validate_database(restored.destination_path).schema_revision == CURRENT_SCHEMA_REVISION
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        assert {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        } == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_fingerprint", b"\x00" * 32),
        ("response_etag", '"page-v2-' + "0" * 64 + '"'),
        ("actor_home_library_id", "1" * 32),
        ("original_request_id", "req_" + "f" * 32),
        ("response_location", "/api/v1/sections/old"),
    ],
)
def test_exact_lifecycle_receipt_mismatch_rejected(
    content_engine: Engine, tmp_path: Path, field: str, value: str | bytes
) -> None:
    _deleted(content_engine)
    bundle = _create(content_engine, tmp_path / "bundle")
    if isinstance(value, bytes):
        _tamper(
            bundle.database_path,
            "trg_idempotency_records_immutable_update",
            f"UPDATE idempotency_records SET {field}=zeroblob(32) WHERE route_template=?",
            ("/api/v1/libraries/{library_id}/pages/{page_id}",),
        )
    else:
        _tamper(
            bundle.database_path,
            "trg_idempotency_records_immutable_update",
            f"UPDATE idempotency_records SET {field}=? WHERE route_template=?",
            (value, "/api/v1/libraries/{library_id}/pages/{page_id}"),
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


def test_orphan_new_audit_is_not_accepted_as_history(
    content_engine: Engine, tmp_path: Path
) -> None:
    _orphan_audit(content_engine)
    with pytest.raises(BackupDatabaseError):
        _create(content_engine, tmp_path / "bundle")


@pytest.mark.parametrize("corruption", ["missing", "duplicate"])
def test_audit_event_receipt_reverse_binding(
    content_engine: Engine, tmp_path: Path, corruption: str
) -> None:
    _deleted(content_engine)
    bundle = _create(content_engine, tmp_path / "bundle")
    if corruption == "missing":
        _tamper(
            bundle.database_path,
            "trg_idempotency_records_no_delete",
            "DELETE FROM idempotency_records WHERE route_template=?",
            ("/api/v1/libraries/{library_id}/pages/{page_id}",),
        )
    else:
        with closing(sqlite3.connect(bundle.database_path)) as connection, connection:
            cursor = connection.execute(
                "SELECT * FROM idempotency_records WHERE route_template=?",
                ("/api/v1/libraries/{library_id}/pages/{page_id}",),
            )
            names = [column[0] for column in cursor.description]
            values = list(cursor.fetchone())
            values[names.index("key_digest")] = b"z" * 32
            connection.execute(
                f"INSERT INTO idempotency_records ({','.join(names)}) "
                f"VALUES ({','.join('?' for _ in names)})",
                values,
            )
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)


@pytest.mark.parametrize("revision", ["20261001_0028", "20261002_0029"])
def test_exact_old_move_backups_restore_and_upgrade_without_relabeling(
    content_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    story = _story(content_engine)
    page = _page(content_engine, story.command.library_id, story.command.page_id)
    target = _target(content_engine, page)
    if revision == "20261001_0028":
        MasterPageMoveService(content_engine).move_page(
            _command(page, target),
            _key("master"),
            master_session=story.session,
        )
    else:
        agent = _agent(content_engine, page.library_id)
        _run_move(content_engine, agent, _move_command(page, target))
    assert content_engine.url.database is not None
    source = Path(content_engine.url.database)
    config = _config(source, monkeypatch)
    command.downgrade(config, revision)
    assert validate_database(source, schema_revision=revision).schema_revision == revision
    with closing(sqlite3.connect(source)) as connection:
        historical = connection.execute("SELECT * FROM page_move_events").fetchall()
    bundle = tmp_path / "old-bundle"
    manifest = replace(_historical_bundle(source, bundle), schema_revision=revision)
    (bundle / "manifest.json").write_bytes(manifest.canonical_bytes())
    restored = restore_backup(
        bundle, tmp_path / "old-restored.sqlite", app_version=APP_VERSION, schema_revision=revision
    )
    assert restored.destination_path.read_bytes() == (bundle / "database.sqlite").read_bytes()
    command.upgrade(config, "head")
    assert validate_database(source).schema_revision == CURRENT_SCHEMA_REVISION
    with closing(sqlite3.connect(source)) as connection:
        rows = connection.execute("SELECT * FROM page_move_events").fetchall()
        assert ([row[:-1] for row in rows] if revision == "20261001_0028" else rows) == historical
