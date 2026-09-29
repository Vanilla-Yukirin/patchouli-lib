from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewCredential
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup import BACKUP_FILENAME, BackupDatabaseError, validate_database
from patchouli_lib.backup import validation as backup_validation
from patchouli_lib.backup.manifest import (
    FILE_SET_SCHEMA_REVISION,
    INTERMEDIATE_SCHEMA_REVISION,
    PREVIOUS_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
)
from patchouli_lib.content.file_manifest import build_file_manifest
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveIdempotencyKey,
    ArchiveMutationSuccess,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    CreateArchiveCommand,
)
from patchouli_lib.content.service import ArchiveService
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import digest_idempotency_key
from patchouli_lib.identifiers import parse_occurrence_time

from .test_service import _append_binary_file_set, _create, _legacy_bundle_with_binary_file


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


def _legacy_database_copy(
    complete_engine: Engine,
    tmp_path: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    bundle = tmp_path / f"legacy-bundle-{name}"
    _legacy_bundle_with_binary_file(complete_engine, bundle, monkeypatch)
    return bundle / BACKUP_FILENAME


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
        guard_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' "
            "AND name = 'trg_pages_occurrence_require_guard'"
        ).fetchone()
        assert guard_row is not None and isinstance(guard_row[0], str)
        connection.execute("DROP TRIGGER trg_pages_occurrence_require_guard")
        _replace_trigger(
            connection,
            "trg_pages_stable_identity",
            "UPDATE pages SET occurred_at = occurred_at + 1000",
        )
        connection.execute(guard_row[0])
        connection.commit()
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


@pytest.mark.parametrize(
    "replacement",
    [
        None,
        "CREATE TRIGGER trg_revision_file_sets_no_replace "
        "BEFORE INSERT ON revision_file_sets BEGIN SELECT 1; END",
    ],
)
def test_0013_backup_requires_exact_file_set_no_replace_trigger(
    complete_engine: Engine,
    tmp_path: Path,
    replacement: str | None,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "file-set-replace-trigger")
    validate_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TRIGGER trg_revision_file_sets_no_replace")
        if replacement is not None:
            connection.execute(replacement)
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


def test_validation_accepts_server_defaulted_occurrence_and_rejects_forged_marker(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    operation_at = parse_occurrence_time("2026-08-13T10:00:01.000000Z").utc_microseconds
    token = generate_token()
    with immediate_transaction(complete_engine) as connection:
        AuthRepository(connection).add_credential(
            NewCredential(
                id="9" * 32,
                library_id="1" * 32,
                caller_id="b" * 32,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=operation_at + 10_000_000,
                created_at=operation_at - 1_000_000,
                updated_at=operation_at - 1_000_000,
            )
        )
        created = ArchiveService(connection, clock=lambda: operation_at).create_archive(
            token.value,
            CreateArchiveCommand(
                library_id="1" * 32,
                section_id="2" * 32,
                book_id="3" * 32,
                title="Defaulted Synthetic Page",
                content_md=b"# Defaulted synthetic page\n",
                source=ArchiveSourceInput(kind="synthetic"),
                request_id="req_" + "a" * 32,
            ),
            ArchiveIdempotencyKey(
                key_digest=digest_idempotency_key("defaulted-backup-synthetic-key")
            ),
        )
        assert isinstance(created, ArchiveMutationSuccess)

    database = _database_copy(complete_engine, tmp_path, "valid-defaulted")
    validate_database(database)

    with immediate_transaction(complete_engine) as connection:
        appended = ArchiveService(connection, clock=lambda: operation_at + 100).append_revision(
            token.value,
            AppendArchiveRevisionCommand(
                library_id="1" * 32,
                section_id="2" * 32,
                page_id=created.page.page_id,
                expected_etag=created.response.response_etag,
                content_md=b"# Later synthetic revision\n",
                source=ArchiveSourceInput(kind="synthetic"),
                request_id="req_" + "c" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("defaulted-later-revision")),
        )
        assert isinstance(appended, ArchiveMutationSuccess)
    with immediate_transaction(complete_engine) as connection:
        corrected = ArchiveService(connection, clock=lambda: operation_at + 200).correct_occurrence(
            token.value,
            CorrectArchiveOccurrenceCommand(
                library_id="1" * 32,
                section_id="2" * 32,
                page_id=created.page.page_id,
                expected_etag=appended.response.response_etag,
                occurred_at=operation_at + 1_000_000,
                request_id="req_" + "d" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("defaulted-correction")),
        )
        assert corrected.response_status == 200
    validate_database(_database_copy(complete_engine, tmp_path, "defaulted-after-edits"))

    with closing(sqlite3.connect(database)) as connection:
        old_body = connection.execute(
            "SELECT response_body FROM idempotency_records WHERE response_etag LIKE ?",
            ('"page-v1-%',),
        ).fetchone()
        assert old_body is not None
        forged = json.loads(old_body[0])
        forged["occurrence_notice"] = {
            "source": "server_utc",
            "warning_code": "occurred_at_defaulted",
        }
        _replace_trigger(
            connection,
            "trg_idempotency_records_immutable_update",
            "UPDATE idempotency_records SET response_body = x'"
            + json.dumps(forged, separators=(",", ":")).encode().hex()
            + "' WHERE response_etag LIKE '\"page-v1-%'",
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


@pytest.mark.parametrize(
    ("name", "trigger", "statement"),
    [
        (
            "missing-markdown",
            "trg_revision_files_no_delete",
            "DELETE FROM revision_files WHERE revision_number = 1 AND filename = 'content.md'",
        ),
        (
            "bad-binary-digest",
            "trg_revision_files_no_update",
            "UPDATE revision_files SET content_sha256 = zeroblob(32) WHERE filename = 'image.bin'",
        ),
        (
            "bad-binary-size",
            "trg_revision_files_no_update",
            "UPDATE revision_files SET size_bytes = size_bytes + 1 WHERE filename = 'image.bin'",
        ),
    ],
)
def test_validation_rejects_missing_content_md_or_corrupt_recorded_files(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    trigger: str,
    statement: str,
) -> None:
    database = _legacy_database_copy(complete_engine, tmp_path, name, monkeypatch)
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(connection, trigger, statement, ignore_checks=True)
    with pytest.raises(BackupDatabaseError):
        validate_database(database, schema_revision="20260929_0007")


def test_validation_rejects_legacy_markdown_and_file_snapshot_divergence(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "diverged-markdown")
    forged = b"# Different but internally valid Markdown\n"
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            "trg_revision_files_no_update",
            "UPDATE revision_files SET "
            f"content_bytes = x'{forged.hex()}', size_bytes = {len(forged)}, "
            f"content_sha256 = x'{hashlib.sha256(forged).hexdigest()}' "
            "WHERE filename = 'content.md'",
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize(
    ("name", "trigger", "statement"),
    [
        (
            "missing-file",
            "trg_revision_files_no_delete",
            "DELETE FROM revision_files WHERE revision_number = 2 AND filename = 'chart.png'",
        ),
        (
            "bad-file-digest",
            "trg_revision_files_no_update",
            "UPDATE revision_files SET content_sha256 = zeroblob(32) "
            "WHERE revision_number = 2 AND filename = 'chart.png'",
        ),
        (
            "bad-file-size",
            "trg_revision_files_no_update",
            "UPDATE revision_files SET size_bytes = size_bytes + 1 "
            "WHERE revision_number = 2 AND filename = 'chart.png'",
        ),
        (
            "noncanonical-name",
            "trg_revision_files_no_update",
            "UPDATE revision_files SET filename = 'Café.bin' "
            "WHERE revision_number = 2 AND filename = 'chart.png'",
        ),
        (
            "bad-snapshot-digest",
            "trg_revision_file_sets_no_update",
            "UPDATE revision_file_sets SET snapshot_sha256 = zeroblob(32) "
            "WHERE revision_number = 2",
        ),
        (
            "bad-file-count",
            "trg_revision_file_sets_no_update",
            "UPDATE revision_file_sets SET file_count = 1 WHERE revision_number = 2",
        ),
        (
            "bad-total-size",
            "trg_revision_file_sets_no_update",
            "UPDATE revision_file_sets SET total_size_bytes = total_size_bytes + 1 "
            "WHERE revision_number = 2",
        ),
        (
            "missing-manifest",
            "trg_revision_file_sets_no_delete",
            "DELETE FROM revision_file_sets WHERE revision_number = 2",
        ),
    ],
)
def test_0013_validation_rejects_inconsistent_binary_snapshot(
    complete_engine: Engine,
    tmp_path: Path,
    name: str,
    trigger: str,
    statement: str,
) -> None:
    _append_binary_file_set(complete_engine)
    database = _database_copy(complete_engine, tmp_path, name)
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(connection, trigger, statement, ignore_checks=True)
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_0013_validation_requires_source_for_binary_revision(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    _append_binary_file_set(complete_engine)
    database = _database_copy(complete_engine, tmp_path, "missing-binary-source")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM page_sources WHERE revision_number = 2")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_0013_rejects_noncanonical_stored_name_with_coordinated_digest(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    _revision_id, files = _append_binary_file_set(complete_engine)
    database = _database_copy(complete_engine, tmp_path, "normalized-name-forgery")
    decomposed = "Café.bin"
    assert decomposed != "Café.bin"
    forged = build_file_manifest([(decomposed, files[0][1]), files[1]])
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            "trg_revision_files_no_update",
            "UPDATE revision_files SET filename = 'Café.bin' "
            "WHERE revision_number = 2 AND filename = 'chart.png'",
        )
        _replace_trigger(
            connection,
            "trg_revision_file_sets_no_update",
            "UPDATE revision_file_sets SET snapshot_sha256 = "
            f"x'{forged.snapshot_sha256.hex()}' WHERE revision_number = 2",
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_0013_rejects_oversize_file_set_before_building_manifest(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _append_binary_file_set(complete_engine)
    database = _database_copy(complete_engine, tmp_path, "oversize-binary-snapshot")
    file_size = 16 * 1024 * 1024
    digest = hashlib.sha256(bytes(file_size)).digest()
    with closing(sqlite3.connect(database)) as connection:
        protected = (
            "trg_revision_files_no_delete",
            "trg_revision_files_legacy_sealed_insert",
        )
        triggers = [
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (name,)
            ).fetchone()[0]
            for name in protected
        ]
        for name in protected:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DELETE FROM revision_files WHERE revision_number = 2")
        for index in range(5):
            connection.execute(
                "INSERT INTO revision_files "
                "(library_id, page_uid, revision_id, revision_number, filename, "
                "content_bytes, size_bytes, content_sha256) "
                "SELECT library_id, page_uid, revision_id, revision_number, ?, "
                "zeroblob(?), ?, ? FROM revisions WHERE revision_number = 2",
                (f"file-{index:02d}.bin", file_size, file_size, digest),
            )
        for statement in triggers:
            connection.execute(statement)
        connection.commit()

    builder_file_counts: list[int] = []

    def bounded_builder(files: list[tuple[str, bytes]]) -> object:
        builder_file_counts.append(len(files))
        if len(files) == 5:
            pytest.fail("Over-limit files reached the manifest builder.")
        return build_file_manifest(files)

    monkeypatch.setattr(backup_validation, "build_file_manifest", bounded_builder)
    with pytest.raises(BackupDatabaseError):
        validate_database(database)
    assert 5 not in builder_file_counts


@pytest.mark.parametrize("name", ["CONTENT.MD", "bad\u2028name"])
def test_validation_rejects_ambiguous_or_unsafe_file_names(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    database = _legacy_database_copy(complete_engine, tmp_path, "bad-file-name", monkeypatch)
    content = b"synthetic"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO revision_files (library_id, page_uid, revision_id, "
            "revision_number, filename, content_bytes, size_bytes, content_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, ?, ?, ?, ? "
            "FROM revisions WHERE revision_number = 1",
            (name, content, len(content), hashlib.sha256(content).digest()),
        )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database, schema_revision="20260929_0007")


@pytest.mark.parametrize(
    ("trigger", "table"),
    [
        ("trg_revision_file_seals_no_delete", "revision_file_seals"),
        ("trg_revision_file_seal_guards_no_delete", "revision_file_seal_guards"),
    ],
)
def test_0008_validation_rejects_missing_seal_or_guard(
    complete_engine: Engine,
    tmp_path: Path,
    trigger: str,
    table: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, f"missing-{table}")
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(connection, trigger, f"DELETE FROM {table}")
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize(
    "schema_revision",
    [
        PREVIOUS_SCHEMA_REVISION,
        INTERMEDIATE_SCHEMA_REVISION,
        FILE_SET_SCHEMA_REVISION,
        SUPPORTED_SCHEMA_REVISION,
    ],
)
def test_sealed_validation_rejects_extra_file_even_if_write_triggers_were_bypassed(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_revision: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "extra-sealed-file")
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    if schema_revision != SUPPORTED_SCHEMA_REVISION:
        command.downgrade(
            Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")),
            schema_revision,
        )
    validate_database(database, schema_revision=schema_revision)
    content = b"synthetic binary"
    names = (
        "trg_revision_files_legacy_sealed_insert",
        "trg_revision_files_auto_seal_legacy",
    )
    with closing(sqlite3.connect(database)) as connection:
        trigger_sql = [
            connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?", (name,)
            ).fetchone()[0]
            for name in names
        ]
        for name in names:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(
            "INSERT INTO revision_files (library_id, page_uid, revision_id, "
            "revision_number, filename, content_bytes, size_bytes, content_sha256) "
            "SELECT library_id, page_uid, revision_id, revision_number, "
            "'image.bin', ?, ?, ? FROM revisions WHERE revision_number = 1",
            (content, len(content), hashlib.sha256(content).digest()),
        )
        for statement in trigger_sql:
            connection.execute(statement)
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database, schema_revision=schema_revision)


@pytest.mark.parametrize(
    ("display_name", "match_key"),
    [("Cafe\u0301", "café"), ("Café", "incorrect")],
)
def test_0010_validation_rejects_noncanonical_tag_names(
    complete_engine: Engine,
    tmp_path: Path,
    display_name: str,
    match_key: str,
) -> None:
    database = _database_copy(complete_engine, tmp_path, "bad-tag")
    validate_database(database)
    with closing(sqlite3.connect(database)) as connection:
        library = connection.execute("SELECT id FROM libraries LIMIT 1").fetchone()
        assert library is not None
        connection.execute(
            "INSERT INTO tags (library_id, id, display_name, match_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (library[0], "a" * 32, display_name, match_key, 1_000_000),
        )
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


@pytest.mark.parametrize("table", ["tags", "page_tags"])
def test_0010_validation_rejects_noninteger_tag_timestamps(
    complete_engine: Engine, tmp_path: Path, table: str
) -> None:
    database = _database_copy(complete_engine, tmp_path, f"bad-{table}-time")
    with closing(sqlite3.connect(database)) as connection:
        page = connection.execute("SELECT library_id, page_uid FROM pages LIMIT 1").fetchone()
        assert page is not None
        library_id, page_uid = page
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO tags (library_id, id, display_name, match_key, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                library_id,
                "a" * 32,
                "Synthetic",
                "synthetic",
                "not-a-time" if table == "tags" else 1,
            ),
        )
        if table == "page_tags":
            connection.execute(
                "INSERT INTO page_tags (library_id, page_uid, tag_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (library_id, page_uid, "a" * 32, "not-a-time"),
            )
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)
