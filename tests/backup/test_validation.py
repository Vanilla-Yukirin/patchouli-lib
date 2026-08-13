from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import Engine

from patchouli_lib.backup import BackupDatabaseError, validate_database

from .test_service import _create


def _replace_trigger(
    connection: sqlite3.Connection,
    name: str,
    statement: str,
    *,
    ignore_checks: bool = False,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (name,),
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    trigger_sql = row[0]
    connection.execute(f"DROP TRIGGER {name}")
    if ignore_checks:
        connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(statement)
    if ignore_checks:
        connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.execute(trigger_sql)
    connection.commit()


def _database_copy(complete_engine: Engine, tmp_path: Path, name: str) -> Path:
    bundle = tmp_path / f"bundle-{name}"
    return _create(complete_engine, bundle).database_path


@pytest.mark.parametrize(
    ("trigger", "statement", "ignore_checks"),
    [
        (
            "trg_revisions_immutable_update",
            "UPDATE revisions SET content_sha256 = zeroblob(32)",
            False,
        ),
        (
            "trg_page_identifier_registry_stable",
            "UPDATE page_identifier_registry SET identifier_digest = zeroblob(32)",
            False,
        ),
        (
            "trg_page_id_collision_counters_monotonic",
            "UPDATE page_id_collision_counters SET next_ordinal = 1",
            True,
        ),
        (
            "trg_idempotency_records_immutable_update",
            "UPDATE idempotency_records SET response_body = x'7b7d'",
            False,
        ),
    ],
)
def test_validation_rejects_digest_counter_and_replay_corruption(
    complete_engine: Engine,
    tmp_path: Path,
    trigger: str,
    statement: str,
    ignore_checks: bool,
) -> None:
    database = _database_copy(complete_engine, tmp_path, trigger)
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            trigger,
            statement,
            ignore_checks=ignore_checks,
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_validation_rejects_page_current_source_and_pending_append_corruption(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    mutations = {
        "current": "UPDATE pages SET current_revision_number = 2",
        "source": "UPDATE page_sources SET revision_id = 'rev_ffffffffffffffffffffffffffffffff'",
        "guard": (
            "INSERT INTO page_revision_append_guards "
            "(library_id, page_uid, revision_id, revision_number) "
            "SELECT library_id, page_uid, revision_id, 1 FROM revisions LIMIT 1"
        ),
    }
    for name, statement in mutations.items():
        database = _database_copy(complete_engine, tmp_path, name)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            if name == "current":
                trigger_sql = connection.execute(
                    "SELECT sql FROM sqlite_schema "
                    "WHERE name = 'trg_pages_current_revision_advance'"
                ).fetchone()[0]
                connection.execute("DROP TRIGGER trg_pages_current_revision_advance")
                connection.execute(statement)
                connection.execute(trigger_sql)
            else:
                connection.execute(statement)
            connection.commit()
        with pytest.raises(BackupDatabaseError):
            validate_database(database)


def test_validation_requires_source_for_every_revision_but_allows_multiple(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    missing = _database_copy(complete_engine, tmp_path, "missing-source")
    with closing(sqlite3.connect(missing)) as connection:
        connection.execute("DELETE FROM page_sources")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(missing)

    multiple = _database_copy(complete_engine, tmp_path, "multiple-sources")
    with closing(sqlite3.connect(multiple)) as connection:
        connection.execute(
            "INSERT INTO page_sources "
            "(library_id, source_id, page_uid, revision_id, revision_number, kind, "
            "locator, captured_at, created_at) SELECT library_id, ?, page_uid, "
            "revision_id, revision_number, kind, NULL, captured_at, created_at "
            "FROM page_sources LIMIT 1",
            ("9" * 32,),
        )
        connection.commit()
    validate_database(multiple)


def test_validation_requires_page_occurrence_and_id_timestamp_alignment(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "occurrence-alignment")
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            "trg_pages_stable_identity",
            "UPDATE pages SET occurred_at = occurred_at + 1000",
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE unexpected_table (value INTEGER)",
        "CREATE VIEW unexpected_view AS SELECT id FROM libraries",
        "CREATE TRIGGER unexpected_trigger AFTER UPDATE ON libraries BEGIN SELECT 1; END",
    ],
)
def test_validation_rejects_unknown_schema_objects(
    complete_engine: Engine,
    tmp_path: Path,
    statement: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "unknown-schema-object")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(statement)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_validation_rejects_recreated_table_with_weakened_constraints(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "weakened-table")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE weak_schema_metadata (key VARCHAR(100), value VARCHAR(500))"
        )
        connection.execute(
            "INSERT INTO weak_schema_metadata SELECT key, value FROM schema_metadata"
        )
        connection.execute("DROP TABLE schema_metadata")
        connection.execute("ALTER TABLE weak_schema_metadata RENAME TO schema_metadata")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_validation_rejects_auth_rotation_bootstrap_and_missing_schema_objects(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    corruptions = {
        "rotation-fan-in": (
            "INSERT INTO auth_credentials "
            "(id, library_id, caller_id, selector, token_version, verifier, expires_at, "
            "created_at, updated_at, last_used_at, revoked_at, rotated_at, "
            "rotated_to_credential_id) "
            "SELECT '99999999999999999999999999999999', library_id, id, "
            "'DDDDDDDDDDDDDDDDDDDDDD', 1, zeroblob(32), 1000, 10, 20, NULL, "
            "20, 20, 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' FROM auth_callers "
            "WHERE kind = 'agent'"
        ),
        "bootstrap-agent": (
            "UPDATE operator_bootstrap_markers SET "
            "operator_caller_id = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', "
            "initial_credential_id = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'"
        ),
        "forged-version": "UPDATE alembic_version SET version_num = 'forged'",
        "missing-trigger": "DROP TRIGGER trg_revisions_immutable_delete",
    }
    for name, statement in corruptions.items():
        database = _database_copy(complete_engine, tmp_path, name)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(statement)
            connection.commit()
        with pytest.raises(BackupDatabaseError):
            validate_database(database)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE auth_credentials SET revoked_at = 21 WHERE rotated_at IS NOT NULL",
        "UPDATE auth_section_grants SET section_id = 'ffffffffffffffffffffffffffffffff'",
        "UPDATE auth_audit_events SET actor_credential_id = 'ffffffffffffffffffffffffffffffff'",
    ],
)
def test_validation_rejects_auth_revocation_grant_and_audit_corruption(
    complete_engine: Engine,
    tmp_path: Path,
    statement: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "auth-graph")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE idempotency_records SET method = 'GET'",
        "UPDATE idempotency_records SET response_status = 200",
        "UPDATE idempotency_records SET response_body = "
        "CAST(replace(CAST(response_body AS TEXT), 'Synthetic Archive', 'Wrong title') "
        "AS BLOB)",
        "UPDATE idempotency_records SET response_body = "
        "CAST(replace(CAST(response_body AS TEXT), '/revisions/1', '/revisions/9') "
        "AS BLOB)",
    ],
)
def test_validation_rejects_semantically_unreconstructable_replays(
    complete_engine: Engine,
    tmp_path: Path,
    statement: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "replay-semantics")
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            "trg_idempotency_records_immutable_update",
            statement,
            ignore_checks=True,
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_validation_rejects_corrupt_and_truncated_sqlite_files(tmp_path: Path) -> None:
    for name, content in (
        ("corrupt.sqlite", b"not sqlite"),
        ("truncated.sqlite", b"SQLite format 3\0"),
    ):
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(BackupDatabaseError):
            validate_database(path)
