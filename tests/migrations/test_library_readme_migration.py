"""Three metadata columns preserve old authority and reject lossy rollback."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from auth.test_optional_expiry_migration import _schema, _snapshot
from backup.conftest import _config, seed_complete_database
from backup.test_service import _create

from patchouli_lib.backup import validate_database, verify_backup_bundle
from patchouli_lib.backup.manifest import (
    LIBRARY_GUIDES_SCHEMA_REVISION,
    ONBOARDING_TICKETS_SCHEMA_REVISION,
)


def test_readme_migration_preserves_old_fields_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "library-guides.sqlite"
    config = _config(path, monkeypatch)
    engine = seed_complete_database(path, monkeypatch)
    engine.dispose()
    command.downgrade(config, ONBOARDING_TICKETS_SCHEMA_REVISION)
    before, old_schema = _snapshot(path), _schema(path)
    validate_database(path, schema_revision=ONBOARDING_TICKETS_SCHEMA_REVISION)
    command.upgrade(config, LIBRARY_GUIDES_SCHEMA_REVISION)
    after = _snapshot(path)
    assert {
        name: [row[:-1] for row in rows] if name in {"libraries", "sections", "books"} else rows
        for name, rows in after.items()
    } == before
    for name in ("libraries", "sections", "books"):
        assert all(row[-1] == "" for row in after[name])
    validate_database(path, schema_revision=LIBRARY_GUIDES_SCHEMA_REVISION)
    command.downgrade(config, ONBOARDING_TICKETS_SCHEMA_REVISION)
    assert _snapshot(path) == before
    assert _schema(path) == old_schema
    command.upgrade(config, LIBRARY_GUIDES_SCHEMA_REVISION)
    with closing(sqlite3.connect(path)) as connection:
        for table in ("libraries", "sections", "books"):
            connection.execute(f"UPDATE {table} SET readme = ?", ("  # Rules\n\n" + "文" * 5000,))
        connection.commit()
    populated = _snapshot(path)
    with pytest.raises(RuntimeError, match="README exists"):
        command.downgrade(config, ONBOARDING_TICKETS_SCHEMA_REVISION)
    assert _snapshot(path) == populated
    validate_database(path, schema_revision=LIBRARY_GUIDES_SCHEMA_REVISION)
    command.upgrade(config, "head")
    command.check(config)
    validate_database(path)
    from patchouli_lib.database import build_engine

    engine = build_engine(f"sqlite:///{path.as_posix()}")
    try:
        bundle = _create(engine, tmp_path / "bundle")
        verify_backup_bundle(bundle.bundle_path, app_version="0.1.0a0")
    finally:
        engine.dispose()
