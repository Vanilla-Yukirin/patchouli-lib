"""0017 backups keep a writer's home Library distinct from a target Library."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from patchouli_lib.backup import (
    BackupDatabaseError,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup.manifest import SUPPORTED_SCHEMA_REVISION
from patchouli_lib.database import immediate_transaction

from .conftest import APP_VERSION
from .test_service import _create

_HOME_LIBRARY = "1" * 32
_TARGET_LIBRARY = "9" * 32
_CROSS_HOME_AUDIT = "8" * 32


def _seed_cross_home_audit(engine: Engine) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            text(
                "INSERT INTO libraries (id, name, created_at, updated_at) "
                "VALUES (:target, 'Another synthetic Library', 30, 30)"
            ),
            {"target": _TARGET_LIBRARY},
        )
        connection.execute(
            text(
                "INSERT INTO auth_audit_events "
                "(id, library_id, actor_home_library_id, actor_caller_id, "
                "actor_credential_id, target_caller_id, section_id, section_action, "
                "action, resource_type, resource_id, outcome, request_id, "
                "policy_version_before, policy_version_after, occurred_at) "
                "VALUES (:id, :target, :home, :actor, :credential, NULL, NULL, NULL, "
                "'admin.library.create', 'library', :target, 'succeeded', "
                "'req_cross_home_backup', NULL, NULL, 30)"
            ),
            {
                "id": _CROSS_HOME_AUDIT,
                "target": _TARGET_LIBRARY,
                "home": _HOME_LIBRARY,
                "actor": "a" * 32,
                "credential": "c" * 32,
            },
        )


def test_cross_home_audit_survives_backup_and_restore(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _seed_cross_home_audit(complete_engine)
    bundle = _create(complete_engine, tmp_path / "cross-home-bundle")
    assert validate_database(bundle.database_path).schema_revision == SUPPORTED_SCHEMA_REVISION
    assert verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION) == bundle.manifest

    restored = tmp_path / "cross-home-restored.sqlite"
    restore_backup(bundle.bundle_path, restored, app_version=APP_VERSION)
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT library_id, actor_home_library_id, actor_caller_id "
            "FROM auth_audit_events WHERE id = ?",
            (_CROSS_HOME_AUDIT,),
        ).fetchone() == (_TARGET_LIBRARY, _HOME_LIBRARY, "a" * 32)


def test_cross_home_actor_corruption_rejected_even_with_rebound_manifest(
    complete_engine: Engine, tmp_path: Path
) -> None:
    _seed_cross_home_audit(complete_engine)
    bundle = _create(complete_engine, tmp_path / "cross-home-corrupted")
    with closing(sqlite3.connect(bundle.database_path)) as connection:
        connection.execute(
            "UPDATE auth_audit_events SET actor_home_library_id = ? WHERE id = ?",
            (_TARGET_LIBRARY, _CROSS_HOME_AUDIT),
        )
        connection.commit()
    forged_bytes = bundle.database_path.read_bytes()
    rebound = replace(
        bundle.manifest,
        byte_size=len(forged_bytes),
        sha256=hashlib.sha256(forged_bytes).hexdigest(),
    )
    bundle.manifest_path.write_bytes(rebound.canonical_bytes())

    with pytest.raises(BackupDatabaseError):
        validate_database(bundle.database_path)
    with pytest.raises(BackupDatabaseError):
        verify_backup_bundle(bundle.bundle_path, app_version=APP_VERSION)
    destination = tmp_path / "rejected-restore.sqlite"
    with pytest.raises(BackupDatabaseError):
        restore_backup(bundle.bundle_path, destination, app_version=APP_VERSION)
    assert not destination.exists()
