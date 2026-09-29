"""Backup acceptance and corruption checks for the Page lifecycle schema."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import NewCredential
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    OCCURRENCE_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.content.schemas import (
    AppendArchiveRevisionCommand,
    ArchiveIdempotencyKey,
    ArchiveMutationSuccess,
    ArchiveSourceInput,
    CorrectArchiveOccurrenceCommand,
    PageLifecycleCommand,
)
from patchouli_lib.content.service import ArchiveService, page_current_etag
from patchouli_lib.database import immediate_transaction
from patchouli_lib.idempotency.schemas import OriginalResponse, digest_idempotency_key
from patchouli_lib.identifiers import parse_occurrence_time

from .conftest import APP_VERSION, _config
from .test_service import _create, identity

_LIBRARY_ID = "1" * 32
_SECTION_ID = "2" * 32
_CALLER_ID = "b" * 32
_TIME = parse_occurrence_time("2026-08-13T10:00:01.000000Z").utc_microseconds


def _credential(engine: Engine) -> str:
    token = generate_token()
    with immediate_transaction(engine) as connection:
        AuthRepository(connection).add_credential(
            NewCredential(
                id="9" * 32,
                library_id=_LIBRARY_ID,
                caller_id=_CALLER_ID,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=_TIME + 10_000_000,
                created_at=_TIME - 1_000_000,
                updated_at=_TIME - 1_000_000,
            )
        )
    return token.value


def _page(engine: Engine) -> tuple[str, str, int, str, int, bytes]:
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT page_id, current_revision_id, current_revision_number, "
            "page_type, occurred_at, page_uid FROM pages"
        ).one()
        assert row[3] == "archive"
        return row[0], row[1], row[2], row[3], row[4], row[5]


def _lifecycle(
    engine: Engine,
    *,
    token: str,
    page_id: str,
    etag: str,
    action: Literal["delete", "restore"],
    suffix: str,
    at: int,
) -> OriginalResponse:
    with immediate_transaction(engine) as connection:
        response = ArchiveService(connection, clock=lambda: at).transition_page_lifecycle(
            token,
            PageLifecycleCommand(
                library_id=_LIBRARY_ID,
                section_id=_SECTION_ID,
                page_id=page_id,
                expected_etag=etag,
                request_id="req_" + suffix * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key(f"lifecycle-{suffix}")),
            action=action,
        )
        assert isinstance(response, OriginalResponse)
        return response


def _replace_trigger(
    connection: sqlite3.Connection,
    trigger: str,
    statement: str,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (trigger,),
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(statement)
    connection.execute(row[0])
    connection.commit()


def test_current_bundle_preserves_trashed_and_restored_history(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    token = _credential(complete_engine)
    page_id, revision_id, number, _kind, occurrence, page_uid = _page(complete_engine)
    before = page_current_etag(page_uid, revision_id, number, occurrence, 2_000_000)
    deleted = _lifecycle(
        complete_engine,
        token=token,
        page_id=page_id,
        etag=before,
        action="delete",
        suffix="a",
        at=_TIME,
    )
    first_bundle = _create(complete_engine, tmp_path / "deleted-bundle")
    assert verify_backup_bundle(first_bundle.bundle_path, app_version=APP_VERSION) == (
        first_bundle.manifest
    )
    first_restore = restore_backup(
        first_bundle.bundle_path,
        tmp_path / "deleted-restore.sqlite",
        app_version=APP_VERSION,
    )
    with closing(sqlite3.connect(first_restore.destination_path)) as database:
        assert database.execute("SELECT deleted_at FROM pages").fetchone() == (_TIME,)
        assert database.execute("SELECT count(*) FROM revisions").fetchone() == (1,)

    restored = _lifecycle(
        complete_engine,
        token=token,
        page_id=page_id,
        etag=deleted.response_etag,
        action="restore",
        suffix="c",
        at=_TIME,  # the persisted Page clock must still advance
    )
    with immediate_transaction(complete_engine) as connection:
        appended = ArchiveService(connection, clock=lambda: _TIME + 100).append_revision(
            token,
            AppendArchiveRevisionCommand(
                library_id=_LIBRARY_ID,
                section_id=_SECTION_ID,
                page_id=page_id,
                expected_etag=restored.response_etag,
                content_md=b"# Revised after restore\n",
                source=ArchiveSourceInput(kind="synthetic"),
                request_id="req_" + "d" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("after-restore-revision")),
        )
        assert isinstance(appended, ArchiveMutationSuccess)
    with immediate_transaction(complete_engine) as connection:
        corrected = ArchiveService(connection, clock=lambda: _TIME + 200).correct_occurrence(
            token,
            CorrectArchiveOccurrenceCommand(
                library_id=_LIBRARY_ID,
                section_id=_SECTION_ID,
                page_id=page_id,
                expected_etag=appended.response.response_etag,
                occurred_at=occurrence + 1_000_000,
                request_id="req_" + "e" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("after-restore-occurrence")),
        )
        assert corrected.response_status == 200
    final_bundle = _create(complete_engine, tmp_path / "restored-bundle")
    assert verify_backup_bundle(final_bundle.bundle_path, app_version=APP_VERSION) == (
        final_bundle.manifest
    )
    final_restore = restore_backup(
        final_bundle.bundle_path,
        tmp_path / "restored.sqlite",
        app_version=APP_VERSION,
    )
    with closing(sqlite3.connect(final_restore.destination_path)) as database:
        assert database.execute("SELECT deleted_at FROM pages").fetchone() == (None,)
        assert database.execute("SELECT count(*) FROM page_lifecycle_events").fetchone() == (2,)
        assert database.execute("SELECT count(*) FROM revisions").fetchone() == (2,)


@pytest.mark.parametrize(
    ("trigger", "statement"),
    [
        (
            "trg_page_lifecycle_events_no_update",
            "UPDATE page_lifecycle_events SET old_updated_at = old_updated_at + 1",
        ),
        (
            "trg_page_lifecycle_events_no_update",
            "UPDATE page_lifecycle_events SET at_revision_number = 2",
        ),
        (
            "trg_page_lifecycle_events_no_delete",
            "DELETE FROM page_lifecycle_events",
        ),
        (
            "trg_page_lifecycle_guards_validate_insert",
            "INSERT INTO page_lifecycle_guards SELECT * FROM page_lifecycle_events",
        ),
        (
            "trg_idempotency_records_immutable_update",
            "UPDATE idempotency_records SET response_etag = '"
            + '"page-v2-'
            + "0" * 64
            + '"'
            + "' WHERE method = 'DELETE'",
        ),
        (
            "trg_idempotency_records_no_delete",
            "DELETE FROM idempotency_records WHERE method = 'DELETE'",
        ),
        (
            "",
            "DELETE FROM auth_audit_events WHERE action = 'content.archive.delete'",
        ),
    ],
)
def test_backup_rejects_corrupt_lifecycle_chain_or_replay(
    complete_engine: Engine,
    tmp_path: Path,
    trigger: str,
    statement: str,
) -> None:
    token = _credential(complete_engine)
    page_id, revision_id, number, _kind, occurrence, page_uid = _page(complete_engine)
    _lifecycle(
        complete_engine,
        token=token,
        page_id=page_id,
        etag=page_current_etag(page_uid, revision_id, number, occurrence, 2_000_000),
        action="delete",
        suffix="a",
        at=_TIME,
    )
    bundle = _create(complete_engine, tmp_path / "source-bundle")
    database = bundle.database_path
    with closing(sqlite3.connect(database)) as connection:
        if trigger:
            _replace_trigger(connection, trigger, statement)
        else:
            connection.execute(statement)
            connection.commit()
    with pytest.raises(BackupDatabaseError):
        validate_database(database)


def test_old_0011_correction_bundle_requires_explicit_revision(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database = complete_engine.url.database
    assert source_database is not None
    source_path = Path(source_database)
    command.downgrade(_config(source_path, monkeypatch), OCCURRENCE_SCHEMA_REVISION)
    token = _credential(complete_engine)
    page_id, revision_id, number, _kind, occurrence, page_uid = _page(complete_engine)
    with immediate_transaction(complete_engine) as connection:
        corrected = ArchiveService(connection, clock=lambda: _TIME).correct_occurrence(
            token,
            CorrectArchiveOccurrenceCommand(
                library_id=_LIBRARY_ID,
                section_id=_SECTION_ID,
                page_id=page_id,
                expected_etag=page_current_etag(
                    page_uid, revision_id, number, occurrence, 2_000_000
                ),
                occurred_at=occurrence + 1_000_000,
                request_id="req_" + "f" * 32,
            ),
            ArchiveIdempotencyKey(key_digest=digest_idempotency_key("old-correction")),
        )
        assert corrected.response_status == 200

    bundle = tmp_path / "old-0011-bundle"
    bundle.mkdir()
    database = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source_path)) as source:
        journal_mode = source.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database)) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode = DELETE")
            destination.commit()
    data = database.read_bytes()
    artifact = identity()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-08-13T12:34:56.123456Z",
        app_version=APP_VERSION,
        schema_revision=OCCURRENCE_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity=artifact.identity,
        artifact_digest=artifact.digest,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    assert (
        verify_backup_bundle(
            bundle, app_version=APP_VERSION, schema_revision=OCCURRENCE_SCHEMA_REVISION
        )
        == manifest
    )
    result = restore_backup(
        bundle,
        tmp_path / "old-0011-restored.sqlite",
        app_version=APP_VERSION,
        schema_revision=OCCURRENCE_SCHEMA_REVISION,
    )
    assert (
        validate_database(
            result.destination_path, schema_revision=OCCURRENCE_SCHEMA_REVISION
        ).schema_revision
        == OCCURRENCE_SCHEMA_REVISION
    )
    with closing(sqlite3.connect(database)) as connection:
        _replace_trigger(
            connection,
            "trg_page_occurrence_corrections_no_update",
            "UPDATE page_occurrence_corrections SET old_occurred_at = old_occurred_at + 1",
        )
    with pytest.raises(BackupDatabaseError):
        validate_database(database, schema_revision=OCCURRENCE_SCHEMA_REVISION)
