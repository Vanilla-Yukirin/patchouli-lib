"""Audited Page titles preserve old responses and the exact backup clock."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.backup import BackupDatabaseError, restore_backup, validate_database
from patchouli_lib.backup.manifest import (
    AUDIT_ACTOR_INDEX_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
)
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.file_set_write_service import FILE_SET_APPEND_ROUTE_TEMPLATE
from patchouli_lib.content.service import page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.identifiers import canonical_utc_wire

from .conftest import APP_VERSION, _config
from .test_service import _create

_LIBRARY_ID = "1" * 32
_ACTOR_ID = "9" * 32


def _path(engine: Engine) -> Path:
    database = engine.url.database
    assert database is not None
    return Path(database)


def _edit_title(engine: Engine, title: str, at: int, audit_id: str) -> tuple[bytes, str, int]:
    with immediate_transaction(engine) as connection:
        page_uid, revision_id, number = connection.exec_driver_sql(
            "SELECT page_uid, current_revision_id, current_revision_number FROM pages"
        ).one()
        MasterAuditRepository(connection).add_success(
            identity_id=_ACTOR_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.page.title.edit",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=at,
            event_id=audit_id,
        )
        connection.exec_driver_sql(
            "UPDATE pages SET title = ?, updated_at = ? WHERE library_id = ? AND page_uid = ?",
            (title, at, _LIBRARY_ID, page_uid),
        )
        return page_uid, revision_id, number


def test_title_history_keeps_legacy_replay_and_round_trip(
    complete_engine: Engine, tmp_path: Path
) -> None:
    with complete_engine.connect() as connection:
        original = connection.exec_driver_sql(
            "SELECT response_body, response_etag FROM idempotency_records"
        ).one()
        page_id, revision_id, number, occurrence, page_uid = connection.exec_driver_sql(
            "SELECT page_id, current_revision_id, current_revision_number, occurred_at, page_uid "
            "FROM pages"
        ).one()
    before = page_current_etag(page_uid, revision_id, number, occurrence, 2_000_000)
    _edit_title(complete_engine, "更名后的标题", 2_000_001, "a" * 32)
    middle = page_current_etag(page_uid, revision_id, number, occurrence, 2_000_001)
    _edit_title(complete_engine, "Synthetic Archive", 2_000_002, "b" * 32)
    after = page_current_etag(page_uid, revision_id, number, occurrence, 2_000_002)
    assert len({before, middle, after}) == 3
    with complete_engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT response_body, response_etag FROM idempotency_records"
            ).one()
            == original
        )
        assert connection.exec_driver_sql(
            "SELECT page_id, current_revision_id, current_revision_number FROM pages"
        ).one() == (page_id, revision_id, number)
        assert [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT old_title, new_title, old_updated_at, changed_at "
                "FROM page_title_events ORDER BY sequence"
            ).all()
        ] == [
            ("Synthetic Archive", "更名后的标题", 2_000_000, 2_000_001),
            ("更名后的标题", "Synthetic Archive", 2_000_001, 2_000_002),
        ]
    assert validate_database(_path(complete_engine)).schema_revision == SUPPORTED_SCHEMA_REVISION
    bundle = _create(complete_engine, tmp_path / "title-bundle")
    restored = restore_backup(
        bundle.bundle_path, tmp_path / "restored.sqlite", app_version=APP_VERSION
    )
    assert validate_database(restored.destination_path).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_title_trigger_requires_unused_exact_audit_and_rollback(complete_engine: Engine) -> None:
    with pytest.raises(IntegrityError), immediate_transaction(complete_engine) as connection:
        connection.exec_driver_sql("UPDATE pages SET title = 'unaudited', updated_at = 2000001")
    with pytest.raises(IntegrityError), immediate_transaction(complete_engine) as connection:
        page_uid = connection.exec_driver_sql("SELECT page_uid FROM pages").scalar_one()
        MasterAuditRepository(connection).add_success(
            identity_id=_ACTOR_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.page.title.edit",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=2_000_001,
            event_id="c" * 32,
        )
        connection.exec_driver_sql(
            "UPDATE pages SET title = 'wrong combined edit', page_type = 'memo', "
            "updated_at = 2000001"
        )
    with complete_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT title, updated_at FROM pages").one() == (
            "Synthetic Archive",
            2_000_000,
        )
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM page_title_events").scalar_one() == 0
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM admin_master_audit_events "
                "WHERE action = 'content.page.title.edit'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("target_suffix", "audit_time", "new_time"),
    [
        ("0" * 32, 2_000_001, 2_000_001),
        ("1" * 32, 2_000_002, 2_000_001),
        ("1" * 32, 2_000_000, 2_000_000),
    ],
)
def test_title_trigger_rejects_wrong_target_time_or_nonmonotonic_clock(
    complete_engine: Engine, target_suffix: str, audit_time: int, new_time: int
) -> None:
    with pytest.raises(IntegrityError), immediate_transaction(complete_engine) as connection:
        MasterAuditRepository(connection).add_success(
            identity_id=_ACTOR_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.page.title.edit",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{target_suffix}",
            occurred_at=audit_time,
            event_id="a" * 32,
        )
        connection.exec_driver_sql(
            "UPDATE pages SET title = 'must fail', updated_at = ?", (new_time,)
        )
    assert validate_database(_path(complete_engine)).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_title_change_etag_is_valid_for_later_unchanged_file_set_replay(
    complete_engine: Engine,
) -> None:
    page_uid, revision_id, number = _edit_title(
        complete_engine, "Updated file-set title", 2_000_001, "d" * 32
    )
    with immediate_transaction(complete_engine) as connection:
        page_id, section_id, occurrence = connection.exec_driver_sql(
            "SELECT page_id, section_id, occurred_at FROM pages"
        ).one()
        content = connection.exec_driver_sql(
            "SELECT content_bytes FROM revision_files WHERE revision_number = 1"
        ).scalar_one()
        manifest = build_file_manifest((("content.md", content),))
        body = {
            "changed": False,
            "section_id": section_id,
            "page_id": page_id,
            "revision_id": revision_id,
            "revision_number": number,
            "snapshot_sha256": manifest.snapshot_sha256.hex(),
            "files": [
                {
                    "filename": "content.md",
                    "size_bytes": len(content),
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
        connection.exec_driver_sql(
            "INSERT INTO idempotency_records "
            "(library_id, actor_home_library_id, caller_id, method, route_template, "
            "key_digest, request_fingerprint, response_status, response_media_type, "
            "response_body, response_location, response_etag, original_request_id, "
            "original_request_timestamp) VALUES (?, ?, ?, 'POST', ?, ?, ?, 200, "
            "'application/json', ?, NULL, ?, ?, ?)",
            (
                _LIBRARY_ID,
                _LIBRARY_ID,
                "b" * 32,
                FILE_SET_APPEND_ROUTE_TEMPLATE,
                b"k" * 32,
                b"f" * 32,
                json.dumps(body, separators=(",", ":")).encode(),
                page_current_etag(page_uid, revision_id, number, occurrence, 2_000_001),
                "req_" + "e" * 32,
                canonical_utc_wire(2_000_010),
            ),
        )
    assert validate_database(_path(complete_engine)).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_title_event_corruption_and_orphan_audit_fail_backup_validation(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _edit_title(complete_engine, "A new title", 2_000_001, "e" * 32)
    bundle = _create(complete_engine, tmp_path / "title-corrupt-bundle")
    with closing(sqlite3.connect(bundle.database_path)) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'trg_page_title_events_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_page_title_events_no_update")
        connection.execute("UPDATE page_title_events SET old_title = 'forged'")
        connection.execute(trigger_sql)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)

    with immediate_transaction(complete_engine) as connection:
        page_uid = connection.exec_driver_sql("SELECT page_uid FROM pages").scalar_one()
        MasterAuditRepository(connection).add_success(
            identity_id=_ACTOR_ID,
            session_generation=1,
            session_fingerprint=b"f" * 32,
            action="content.page.title.edit",
            target_type="page",
            target_id=f"{_LIBRARY_ID}:{page_uid.hex()}",
            occurred_at=2_000_002,
            event_id="f" * 32,
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(_path(complete_engine))


def test_historical_0021_still_validates_after_0022_head(
    complete_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _path(complete_engine)
    config = _config(path, monkeypatch)
    command.downgrade(config, AUDIT_ACTOR_INDEX_SCHEMA_REVISION)
    assert (
        validate_database(path, schema_revision=AUDIT_ACTOR_INDEX_SCHEMA_REVISION).schema_revision
        == AUDIT_ACTOR_INDEX_SCHEMA_REVISION
    )
    command.upgrade(config, SUPPORTED_SCHEMA_REVISION)
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION


def test_0022_downgrade_refuses_to_discard_title_history(
    complete_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_title(complete_engine, "Keep this title history", 2_000_001, "a" * 32)
    path = _path(complete_engine)
    with pytest.raises(RuntimeError, match="Cannot discard recorded Page title changes"):
        command.downgrade(_config(path, monkeypatch), AUDIT_ACTOR_INDEX_SCHEMA_REVISION)
    assert validate_database(path).schema_revision == SUPPORTED_SCHEMA_REVISION
