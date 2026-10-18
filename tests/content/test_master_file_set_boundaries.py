"""Additional synthetic boundaries for master-owned complete file-set writes."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from time import time

import pytest
from alembic import command as alembic
from sqlalchemy import Engine, select

from patchouli_lib.admin.contracts import MasterDeletePageFormInput
from patchouli_lib.admin.file_set_receipt_validation import (
    MasterFileSetReceiptCorruptError,
    validate_master_file_set_receipt,
)
from patchouli_lib.admin.file_set_service import (
    MasterFileSetConflictError,
    MasterFileSetService,
)
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.auth.service import AuthenticationError
from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupManifestError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import (
    MASTER_PAGE_DELETE_SCHEMA_REVISION,
    SUPPORTED_SCHEMA_REVISION,
    BackupManifestV1,
)
from patchouli_lib.content.models import Revision, RevisionFileSet
from patchouli_lib.content.repository import ContentRepository
from patchouli_lib.database import immediate_transaction
from patchouli_lib.retrieval.file_set_read import read_verified_revision_snapshot

from .conftest import OPERATION_TIME, alembic_config
from .helpers import insert_page_graph, page_graph_values
from .test_master_file_set_service import _append, _counts, _current_etag, _key, _setup

_APP_VERSION = "0.1.0a0"


def test_defaulted_occurrence_replay_keeps_original_clock(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    defaulted = create.model_copy(update={"occurred_at": None})
    first = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME).create_page(
        defaulted, _key("defaulted-clock"), master_session=session
    )
    replay = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME + 90).create_page(
        defaulted, _key("defaulted-clock"), master_session=session
    )
    assert first.receipt.original_occurred_at == OPERATION_TIME
    assert first.receipt.original_page_updated_at == OPERATION_TIME
    assert replay.replayed and replay.receipt == first.receipt
    with pytest.raises(MasterFileSetConflictError):
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME + 90).create_page(
            defaulted.model_copy(update={"occurred_at": OPERATION_TIME}),
            _key("defaulted-clock"),
            master_session=session,
        )
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


def test_binary_only_creation_has_no_markdown_mirror(content_engine: Engine) -> None:
    create, session = _setup(content_engine)
    files = (("payload.bin", b"\x00\xff\x81\x00"),)
    result = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME).create_page(
        create.model_copy(update={"files": files}),
        _key("binary-only"),
        master_session=session,
    )
    assert result.receipt.changed == 1 and result.receipt.revision_number == 1
    with content_engine.connect() as connection:
        page = ContentRepository(connection).get_page(create.library_id, result.receipt.page_id)
        assert page is not None
        snapshot = read_verified_revision_snapshot(connection, page, result.receipt.revision_id)
        assert [(item.name, item.content) for item in snapshot.manifest.files] == list(files)
        assert (
            connection.scalar(
                select(Revision.content_md).where(
                    Revision.revision_id == result.receipt.revision_id
                )
            )
            is None
        )
        assert (
            connection.scalar(
                select(RevisionFileSet.storage_format).where(
                    RevisionFileSet.revision_id == result.receipt.revision_id
                )
            )
            == "file_set_v1"
        )


def test_invalid_master_identity_generation_or_expiry_never_writes(
    content_engine: Engine,
) -> None:
    create, session = _setup(content_engine)
    invalid = (
        replace(session, expires_at=int(time()) - 1),
        replace(session, identity_id="f" * 32),
        replace(session, session_generation=session.session_generation + 1),
    )
    service = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
    for candidate in invalid:
        with pytest.raises(AuthenticationError):
            service.create_page(create, _key("auth-boundary"), master_session=candidate)
    assert _counts(content_engine) == (0, 0, 0, 0, 0, 0)
    service.create_page(create, _key("auth-boundary"), master_session=session)
    for candidate in invalid:
        with pytest.raises(AuthenticationError):
            service.create_page(create, _key("auth-boundary"), master_session=candidate)
    assert _counts(content_engine) == (1, 1, 1, 1, 1, 0)


def test_legacy_markdown_changed_to_multifile_keeps_exact_old_snapshot(
    content_engine: Engine,
) -> None:
    create, session = _setup(content_engine)
    old_bytes = b"# Original Markdown\n"
    values = page_graph_values(
        library_id=create.library_id,
        section_id=create.section_id,
        book_id=create.book_id,
        content_md=old_bytes,
    )
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
    old_page, old_revision = values[0], values[1]
    new_files = (("notes.md", b"# Replacement\n"), ("raw.bin", b"\x00\xff"))
    append = _append(
        create,
        old_page.page_id,
        _current_etag(content_engine, create.library_id, old_page.page_id),
        new_files,
    )
    result = MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME).revise_page(
        append, create.book_id, _key("legacy-to-multifile"), master_session=session
    )
    assert result.receipt.changed == 1 and result.receipt.revision_number == 2
    with content_engine.connect() as connection:
        page = ContentRepository(connection).get_page(create.library_id, old_page.page_id)
        assert page is not None and page.current_revision_id == result.receipt.revision_id
        old = read_verified_revision_snapshot(connection, page, old_revision.revision_id)
        new = read_verified_revision_snapshot(connection, page, result.receipt.revision_id)
        assert [(item.name, item.content) for item in old.manifest.files] == [
            ("content.md", old_bytes)
        ]
        assert [(item.name, item.content) for item in new.manifest.files] == sorted(new_files)
        assert old.revision_number == 1 and new.revision_number == 2


def test_receipt_rejects_existing_but_wrong_original_book_or_section(
    content_engine: Engine,
) -> None:
    create, session = _setup(content_engine)
    receipt = (
        MasterFileSetService(content_engine, clock=lambda: OPERATION_TIME)
        .create_page(create, _key("original-path"), master_session=session)
        .receipt
    )
    other_book = "d" * 32
    other_section = "e" * 32
    other_section_book = "f" * 32
    with immediate_transaction(content_engine) as connection:
        connection.exec_driver_sql(
            "INSERT INTO books (id, library_id, section_id, name, summary, created_at, "
            "updated_at) VALUES (?, ?, ?, 'Other Book', '', 1000000, 1000000)",
            (other_book, create.library_id, create.section_id),
        )
        connection.exec_driver_sql(
            "INSERT INTO sections (id, library_id, name, description, created_at, updated_at) "
            "VALUES (?, ?, 'Other Section', '', 1000000, 1000000)",
            (other_section, create.library_id),
        )
        connection.exec_driver_sql(
            "INSERT INTO books (id, library_id, section_id, name, summary, created_at, "
            "updated_at) VALUES (?, ?, ?, 'Section Book', '', 1000000, 1000000)",
            (other_section_book, create.library_id, other_section),
        )
    with content_engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        for change in (
            {"book_id": other_book},
            {"section_id": other_section, "book_id": other_section_book},
        ):
            with pytest.raises(MasterFileSetReceiptCorruptError):
                validate_master_file_set_receipt(raw, receipt.model_copy(update=change))


def test_empty_0026_can_downgrade_and_upgrade_without_receipts(content_engine: Engine) -> None:
    config = alembic_config()
    alembic.downgrade(config, MASTER_PAGE_DELETE_SCHEMA_REVISION)
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == (MASTER_PAGE_DELETE_SCHEMA_REVISION)
        assert (
            connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_schema WHERE name = 'admin_master_file_set_receipts'"
            ).first()
            is None
        )
    alembic.upgrade(config, "head")
    with content_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == (SUPPORTED_SCHEMA_REVISION)
        assert (
            connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_schema WHERE name = 'admin_master_file_set_receipts'"
            ).first()
            is not None
        )


def test_populated_0025_bundle_restores_only_with_explicit_version(
    content_engine: Engine, tmp_path: Path
) -> None:
    create, session = _setup(content_engine)
    values = page_graph_values(
        library_id=create.library_id, section_id=create.section_id, book_id=create.book_id
    )
    with immediate_transaction(content_engine) as connection:
        insert_page_graph(connection, values)
    page_id = values[0].page_id
    AdminActionService(content_engine, clock=lambda: OPERATION_TIME + 10).delete_page_as_master(
        create.library_id,
        create.section_id,
        create.book_id,
        page_id,
        MasterDeletePageFormInput(
            expected_etag=_current_etag(content_engine, create.library_id, page_id),
            confirm_delete="yes",
        ),
        master_session=session,
    )
    alembic.downgrade(alembic_config(), MASTER_PAGE_DELETE_SCHEMA_REVISION)
    source_name = content_engine.url.database
    assert source_name is not None
    source = Path(source_name)
    assert validate_database(source, schema_revision=MASTER_PAGE_DELETE_SCHEMA_REVISION)

    bundle = tmp_path / "populated-0025-bundle"
    bundle.mkdir()
    database_path = bundle / BACKUP_FILENAME
    with closing(sqlite3.connect(source)) as original:
        journal_mode = original.execute("PRAGMA journal_mode").fetchone()[0]
        with closing(sqlite3.connect(database_path)) as copied:
            original.backup(copied)
            copied.execute("PRAGMA journal_mode = DELETE")
            copied.commit()
    data = database_path.read_bytes()
    manifest = BackupManifestV1(
        schema_version=1,
        backup_filename=BACKUP_FILENAME,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at="2026-10-01T12:34:56.123456Z",
        app_version=_APP_VERSION,
        schema_revision=MASTER_PAGE_DELETE_SCHEMA_REVISION,
        sqlite_version=sqlite3.sqlite_version,
        source_journal_mode=journal_mode,
        artifact_identity="synthetic/source",
        artifact_digest="sha256:" + "1" * 64,
    )
    (bundle / MANIFEST_FILENAME).write_bytes(manifest.canonical_bytes())
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=_APP_VERSION)
    with pytest.raises(BackupManifestError):
        restore_backup(bundle, tmp_path / "implicit-restore.sqlite", app_version=_APP_VERSION)
    assert (
        verify_backup_bundle(
            bundle, app_version=_APP_VERSION, schema_revision=MASTER_PAGE_DELETE_SCHEMA_REVISION
        )
        == manifest
    )
    restored = restore_backup(
        bundle,
        tmp_path / "explicit-0025.sqlite",
        app_version=_APP_VERSION,
        schema_revision=MASTER_PAGE_DELETE_SCHEMA_REVISION,
    )
    assert (
        validate_database(
            restored.destination_path, schema_revision=MASTER_PAGE_DELETE_SCHEMA_REVISION
        ).schema_revision
        == MASTER_PAGE_DELETE_SCHEMA_REVISION
    )
    with closing(sqlite3.connect(restored.destination_path)) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            MASTER_PAGE_DELETE_SCHEMA_REVISION,
        )
        assert database.execute("SELECT count(*) FROM pages").fetchone() == (1,)
        assert database.execute("SELECT count(*) FROM page_lifecycle_events").fetchone() == (1,)
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'admin_master_file_set_receipts'"
            ).fetchone()
            is None
        )
