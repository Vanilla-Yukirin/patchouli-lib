"""Ticket migration preserves business data and verifies delegation history."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from auth.test_optional_expiry_migration import _schema, _snapshot
from backup.conftest import APP_VERSION, _config, seed_complete_database
from backup.test_service import _create

from patchouli_lib.admin.contracts import MasterProvisionAgentInput
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.onboarding import OnboardingTicketService
from patchouli_lib.backup import (
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    _ACCEPTED_SCHEMA_REVISIONS,
    LIBRARY_SCOPES_SCHEMA_REVISION,
    ONBOARDING_TICKETS_SCHEMA_REVISION,
)
from patchouli_lib.backup_cli import _parse_command
from patchouli_lib.database import immediate_transaction

TABLE = "auth_onboarding_tickets"
TRIGGER = "trg_auth_onboarding_tickets_consume_once"


def test_service_created_and_redeemed_ticket_remains_valid_in_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "real-service.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    try:
        with immediate_transaction(engine) as connection:
            state = MasterTokenRepository(connection).initialize_from_local_cli("m" * 40, now=50)
        session = MasterAdminSession(
            expires_at=1000,
            csrf_token="synthetic-session",
            identity_id=state.identity_id,
            session_generation=state.session_generation,
        )
        service = OnboardingTicketService(engine, clock=lambda: 100)
        delivered = service.create(master_session=session)
        validate_database(path)
        service.redeem(
            delivered.code,
            MasterProvisionAgentInput(
                agent_name="Ticket backup test",
                never_expires=True,
                read_mode="exclude",
                write_mode="include",
            ),
        )
        validate_database(path)
        bundle = _create(engine, tmp_path / "service-bundle")
        verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION)
    finally:
        engine.dispose()


def _ticket(*, consumed: bool = False) -> dict[str, object]:
    return {
        "id": "9" * 32,
        "code_hash": b"h" * 32,
        "identity_id": "8" * 32,
        "session_generation": 1,
        "session_fingerprint": b"f" * 32,
        "created_at": 10,
        "expires_at": 300_000_010,
        "consumed_at": 20 if consumed else None,
        "credential_id": "e" * 32 if consumed else None,
    }


def _insert(connection: sqlite3.Connection, values: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {TABLE} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _audits(connection: sqlite3.Connection, *, consumed: bool = False) -> None:
    events = [("6" * 32, "auth.onboarding.create", "onboarding_ticket", "9" * 32, 10)]
    if consumed:
        events.append(("7" * 32, "auth.agent.provision", "caller", "b" * 32, 20))
    for event_id, action, target_type, target_id, at in events:
        connection.execute(
            "INSERT INTO admin_master_audit_events "
            "(id, identity_id, session_generation, session_fingerprint, action, "
            "target_type, target_id, occurred_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
            (event_id, "8" * 32, b"f" * 32, action, target_type, target_id, at),
        )


@pytest.mark.parametrize("populated", [False, True])
def test_upgrade_preserves_all_existing_data_and_exact_0032_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    path = tmp_path / "migration.sqlite"
    config = _config(path, monkeypatch)
    if populated:
        engine = seed_complete_database(path, monkeypatch)
        engine.dispose()
        command.downgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    else:
        command.upgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    before, schema = _snapshot(path), _schema(path)
    validate_database(path, schema_revision=LIBRARY_SCOPES_SCHEMA_REVISION)
    command.upgrade(config, "head")
    assert {key: value for key, value in _snapshot(path).items() if key != TABLE} == before
    assert _snapshot(path)[TABLE] == []
    assert validate_database(path).schema_revision == ONBOARDING_TICKETS_SCHEMA_REVISION
    command.check(config)
    command.downgrade(config, LIBRARY_SCOPES_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == schema
    validate_database(path, schema_revision=LIBRARY_SCOPES_SCHEMA_REVISION)


@pytest.mark.parametrize("consumed", [False, True])
def test_existing_tickets_prevent_lossy_downgrade_and_roundtrip_through_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumed: bool
) -> None:
    path = tmp_path / "tickets.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    with closing(sqlite3.connect(path)) as connection:
        _insert(connection, _ticket(consumed=consumed))
        _audits(connection, consumed=consumed)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="history is immutable"):
            connection.execute(f"DELETE FROM {TABLE}")
        connection.rollback()
    before, schema = _snapshot(path), _schema(path)
    with pytest.raises(RuntimeError, match="ticket records"):
        command.downgrade(_config(path, monkeypatch), LIBRARY_SCOPES_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == schema
    try:
        bundle = _create(engine, tmp_path / "bundle")
    finally:
        engine.dispose()
    verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION)
    restored = tmp_path / "restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    assert _snapshot(restored) == before
    validate_database(restored)


def test_database_only_allows_one_consumption_and_preserves_ticket_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "constraints.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        _insert(connection, _ticket())
        _audits(connection, consumed=True)
        connection.commit()
        for assignment in (
            "created_at = 11",
            "expires_at = 300000011",
            "session_generation = 2",
            "code_hash = zeroblob(32)",
            "identity_id = 'bad'",
            "id = 'bad'",
            "consumed_at = 300000010, credential_id = '" + "e" * 32 + "'",
            "consumed_at = 9, credential_id = '" + "e" * 32 + "'",
            "consumed_at = 20",
            "credential_id = '" + "e" * 32 + "'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {TABLE} SET {assignment}")
            connection.rollback()
        connection.execute(f"UPDATE {TABLE} SET consumed_at = 20, credential_id = ?", ("e" * 32,))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE {TABLE} SET consumed_at = NULL, credential_id = NULL")
        connection.rollback()
        duplicate = {**_ticket(consumed=True), "id": "a" * 32, "code_hash": b"x" * 32}
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, duplicate)
        connection.rollback()
    validate_database(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad"},
        {"code_hash": "h" * 32},
        {"identity_id": "bad"},
        {"session_generation": 0},
        {"session_fingerprint": "f" * 32},
        {"created_at": -1},
        {"expires_at": 300_000_011},
        {"consumed_at": 20},
        {"credential_id": "e" * 32},
        {"consumed_at": 300_000_010, "credential_id": "e" * 32},
        {"consumed_at": 21, "credential_id": "e" * 32},
        {"consumed_at": 20, "credential_id": "c" * 32},
    ],
)
def test_backup_rejects_corrupt_ticket_rows_despite_disabled_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    path = tmp_path / "corrupt.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        _insert(connection, {**_ticket(), **changes})
        _audits(connection, consumed=True)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(path)


@pytest.mark.parametrize("consumed", [False, True])
def test_backup_requires_matching_creation_and_consumption_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumed: bool
) -> None:
    path = tmp_path / "unaudited.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        _insert(connection, _ticket(consumed=consumed))
        if consumed:
            _audits(connection)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(path)


@pytest.mark.parametrize("trigger", [TRIGGER, "trg_auth_onboarding_tickets_no_delete"])
def test_backup_requires_exact_ticket_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    path = tmp_path / "schema.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"DROP TRIGGER {trigger}")
    with pytest.raises(BackupDatabaseError):
        validate_database(path)


@pytest.mark.parametrize("corruption", ["orphan", "duplicate", "wrong_type", "wrong_time"])
def test_creation_audits_require_exactly_one_matching_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    path = tmp_path / "audit-corruption.sqlite"
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        _audits(connection)
        if corruption != "orphan":
            _insert(connection, _ticket())
            connection.execute(
                "INSERT INTO admin_master_audit_events "
                "(id, identity_id, session_generation, session_fingerprint, action, "
                "target_type, target_id, occurred_at) "
                "VALUES (?, ?, 1, ?, 'auth.onboarding.create', ?, ?, ?)",
                (
                    "5" * 32,
                    "8" * 32,
                    b"f" * 32,
                    "caller" if corruption == "wrong_type" else "onboarding_ticket",
                    "9" * 32,
                    11 if corruption == "wrong_time" else 10,
                ),
            )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(path)


@pytest.mark.parametrize("revision", sorted(_ACCEPTED_SCHEMA_REVISIONS))
def test_backup_cli_accepts_every_supported_historical_revision(revision: str) -> None:
    parsed = _parse_command(["verify", "--bundle", "synthetic", "--schema-revision", revision])
    assert getattr(parsed, "schema_revision", None) == revision
