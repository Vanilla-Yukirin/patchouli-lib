"""Historical backup features are pinned independently of the operation default."""

from __future__ import annotations

import ast
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from sqlalchemy import Engine

from patchouli_lib.backup import manifest as backup_manifest
from patchouli_lib.backup import service as backup_service
from patchouli_lib.backup import validation as backup_validation
from patchouli_lib.backup.errors import BackupDatabaseError, BackupManifestError
from patchouli_lib.backup.manifest import (
    BACKUP_FILENAME,
    CALLER_PAGE_MOVE_SCHEMA_REVISION,
    LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION,
    MASTER_OCCURRENCE_SCHEMA_REVISION,
    PAGE_MOVE_SCHEMA_REVISION,
)

from .conftest import APP_VERSION, _config
from .test_master_occurrence_backup import (
    _agent_correct,
    _create_file_set,
    _historical_bundle,
    _master_correct,
    _session,
    _source,
)
from .test_page_lifecycle_validation import _TIME, _credential

_FUTURE_DEFAULT = "20261001_0029"


def _future_default_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, ModuleType]:
    """Simulate a changed default at import time without adding a schema format.

    Only the default assignment changes in an in-memory AST. All historical
    tables, hashes and feature definitions execute unchanged from the source.
    The isolated modules and temporary import binding are restored by pytest.
    """

    assert backup_manifest.__file__ is not None
    path = Path(backup_manifest.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defaults = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "SUPPORTED_SCHEMA_REVISION"
    ]
    assert len(defaults) == 1
    defaults[0].value = ast.Constant(value=_FUTURE_DEFAULT)
    ast.fix_missing_locations(tree)
    manifest = ModuleType("_backup_manifest_future_default")
    monkeypatch.setitem(sys.modules, manifest.__name__, manifest)
    exec(compile(tree, str(path), "exec"), manifest.__dict__)
    monkeypatch.setitem(sys.modules, "patchouli_lib.backup.manifest", manifest)

    assert backup_validation.__file__ is not None
    spec = spec_from_file_location("_backup_validation_future_default", backup_validation.__file__)
    assert spec is not None and spec.loader is not None
    validation = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, validation.__name__, validation)
    spec.loader.exec_module(validation)
    return manifest, validation


def test_future_default_does_not_relabel_historical_catalogues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, validation = _future_default_modules(monkeypatch)

    assert manifest.SUPPORTED_SCHEMA_REVISION == _FUTURE_DEFAULT
    assert manifest.MASTER_OCCURRENCE_SCHEMA_REVISION == MASTER_OCCURRENCE_SCHEMA_REVISION
    assert manifest._ACCEPTED_SCHEMA_REVISIONS == backup_manifest._ACCEPTED_SCHEMA_REVISIONS
    assert (
        validation._EXPECTED_SQL_HASHES_BY_REVISION
        == backup_validation._EXPECTED_SQL_HASHES_BY_REVISION
    )
    for name in vars(backup_validation):
        if name.endswith("_REVISIONS"):
            assert getattr(validation, name) == getattr(backup_validation, name)
    assert {
        MASTER_OCCURRENCE_SCHEMA_REVISION,
        PAGE_MOVE_SCHEMA_REVISION,
        CALLER_PAGE_MOVE_SCHEMA_REVISION,
        LIBRARY_PAGE_LIFECYCLE_SCHEMA_REVISION,
    } == validation._MASTER_OCCURRENCE_REVISIONS
    assert MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION not in validation._MASTER_OCCURRENCE_REVISIONS
    assert _FUTURE_DEFAULT not in validation._EXPECTED_SQL_HASHES_BY_REVISION
    assert "MASTER_OCCURRENCE_SCHEMA_REVISION" in manifest.__all__

    fields = {
        "schema_version": 1,
        "backup_filename": BACKUP_FILENAME,
        "byte_size": 4096,
        "sha256": "a" * 64,
        "created_at": "2026-10-01T00:00:00.123456Z",
        "app_version": APP_VERSION,
        "sqlite_version": "3.50.4",
        "source_journal_mode": "delete",
        "artifact_identity": "example.invalid/synthetic-backup",
        "artifact_digest": "sha256:" + "b" * 64,
    }
    for revision in (MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION, MASTER_OCCURRENCE_SCHEMA_REVISION):
        historical = manifest.BackupManifestV1(**fields, schema_revision=revision)
        assert manifest.parse_manifest(historical.canonical_bytes()) == historical
        manifest.require_compatible_manifest(
            historical, app_version=APP_VERSION, schema_revision=revision
        )
        with pytest.raises(BackupManifestError):
            manifest.require_compatible_manifest(historical, app_version=APP_VERSION)
    with pytest.raises(BackupManifestError):
        manifest.BackupManifestV1(**fields, schema_revision=_FUTURE_DEFAULT)


@pytest.mark.parametrize(
    "revision", [MASTER_FILE_SET_RECEIPTS_SCHEMA_REVISION, MASTER_OCCURRENCE_SCHEMA_REVISION]
)
def test_real_historical_bundles_restore_after_default_changes(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
) -> None:
    session = _session(complete_engine)
    page_id = _create_file_set(complete_engine, session)
    _agent_correct(complete_engine, _credential(complete_engine), page_id, at=_TIME + 20)
    bundle = tmp_path / "historical-bundle"
    if revision == MASTER_OCCURRENCE_SCHEMA_REVISION:
        _master_correct(complete_engine, session, page_id, at=_TIME + 30)
        source = _source(complete_engine)
        command.downgrade(_config(source, monkeypatch), revision)
        expected = replace(_historical_bundle(source, bundle), schema_revision=revision)
        (bundle / "manifest.json").write_bytes(expected.canonical_bytes())
    else:
        source = _source(complete_engine)
        command.downgrade(_config(source, monkeypatch), revision)
        expected = _historical_bundle(source, bundle)

    manifest, validation = _future_default_modules(monkeypatch)
    monkeypatch.setattr(backup_service, "validate_database", validation.validate_database)
    monkeypatch.setattr(
        backup_service, "require_compatible_manifest", manifest.require_compatible_manifest
    )
    database = bundle / BACKUP_FILENAME
    assert (
        validation.validate_database(database, schema_revision=revision).schema_revision == revision
    )
    # The hypothetical default is not accepted merely because it is the default.
    with pytest.raises(BackupDatabaseError):
        validation.validate_database(database)
    with pytest.raises(BackupDatabaseError):
        validation.validate_database(database, schema_revision=_FUTURE_DEFAULT)

    verified = backup_service.verify_backup_bundle(
        bundle, app_version=APP_VERSION, schema_revision=revision
    )
    assert verified.canonical_bytes() == expected.canonical_bytes()
    restored = backup_service.restore_backup(
        bundle,
        tmp_path / "restored.sqlite",
        app_version=APP_VERSION,
        schema_revision=revision,
    )
    assert restored.destination_path.read_bytes() == database.read_bytes()
    with closing(sqlite3.connect(restored.destination_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            revision,
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(page_occurrence_corrections)")
        }
        assert ("master_audit_event_id" in columns) == (
            revision == MASTER_OCCURRENCE_SCHEMA_REVISION
        )
        assert connection.execute(
            "SELECT count(*) FROM admin_master_file_set_receipts"
        ).fetchone() == (1,)
