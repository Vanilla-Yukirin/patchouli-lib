"""Synthetic-only checks for the master-session audit substrate."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.auth.models import MasterAuditEvent
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.database import build_engine, immediate_transaction

OLD_REVISION = "20260929_0017"
NEW_REVISION = "20260929_0018"
TOKEN = "synthetic master token for audit migration 0001"
IDENTITY = "a" * 32
EVENT = "b" * 32
TARGET = "c" * 32
FINGERPRINT = b"f" * 32


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    return url, Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def _add_event(connection: Connection, *, event_id: str = EVENT) -> None:
    MasterAuditRepository(connection).add_success(
        identity_id=IDENTITY,
        session_generation=1,
        session_fingerprint=FINGERPRINT,
        action="auth.agent_token.reveal",
        target_type="credential",
        target_id=TARGET,
        occurred_at=1_001,
        event_id=event_id,
    )


def test_0018_upgrade_audit_immutability_and_fail_closed_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "master-audit-migration.sqlite"
    url, config = _config(database, monkeypatch)
    command.upgrade(config, OLD_REVISION)
    assert validate_database(database, schema_revision=OLD_REVISION).schema_revision == OLD_REVISION

    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        assert "admin_master_audit_events" in inspect(engine).get_table_names()
        with immediate_transaction(engine) as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.scalar(select(MasterAuditEvent.id)) is None
            MasterTokenRepository(
                connection, identity_factory=lambda: IDENTITY
            ).initialize_from_local_cli(TOKEN, now=1_000)
            _add_event(connection)
            assert connection.scalar(select(MasterAuditEvent.id)) == EVENT
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("UPDATE admin_master_audit_events SET target_id = :target"),
                    {"target": "d" * 32},
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("DELETE FROM admin_master_audit_events"))
            with pytest.raises(IntegrityError), connection.begin_nested():
                MasterAuditRepository(connection).add_success(
                    identity_id=IDENTITY,
                    session_generation=1,
                    session_fingerprint=FINGERPRINT,
                    action="auth.agent_token.reveal",
                    target_type="credential",
                    target_id="not-a-credential-id",
                    occurred_at=1_001,
                    event_id="d" * 32,
                )
        assert (
            validate_database(database, schema_revision=NEW_REVISION).schema_revision
            == NEW_REVISION
        )
        with pytest.raises(RuntimeError, match="discard master audit events"):
            command.downgrade(config, OLD_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(select(MasterAuditEvent.id)) == EVENT
    finally:
        engine.dispose()


def test_0018_empty_downgrade_and_metadata_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "master-audit-empty.sqlite"
    _, config = _config(database, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    assert validate_database(database, schema_revision=NEW_REVISION).schema_revision == NEW_REVISION
    command.downgrade(config, OLD_REVISION)
    assert validate_database(database, schema_revision=OLD_REVISION).schema_revision == OLD_REVISION
    command.upgrade(config, NEW_REVISION)
    assert validate_database(database, schema_revision=NEW_REVISION).schema_revision == NEW_REVISION


def test_master_audit_insert_rolls_back_with_caller_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "master-audit-rollback.sqlite"
    url, config = _config(database, monkeypatch)
    command.upgrade(config, NEW_REVISION)
    engine = build_engine(url)
    try:
        with pytest.raises(IntegrityError), immediate_transaction(engine) as connection:
            _add_event(connection)
            _add_event(connection)
        with engine.connect() as connection:
            assert connection.scalar(select(MasterAuditEvent.id)) is None
    finally:
        engine.dispose()
