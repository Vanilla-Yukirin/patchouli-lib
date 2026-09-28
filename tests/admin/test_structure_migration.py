from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.database import build_engine
from patchouli_lib.library.schemas import CreateLibraryInput


def test_migrated_admin_audit_is_immutable_and_cannot_be_downgraded_with_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "structure-migration.db"
    database_url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        created = AdminActionService(engine).create_library(
            CreateLibraryInput(name="Synthetic Library"), session_fingerprint=b"s" * 32
        )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT action FROM admin_structure_audit_events WHERE library_id = :id"),
                    {"id": created.id},
                ).scalar_one()
                == "library.create"
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE admin_structure_audit_events SET action = 'section.create'"
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM admin_structure_audit_events")
        with pytest.raises(RuntimeError, match="discard admin structure audit"):
            command.downgrade(config, "20260929_0008")
    finally:
        engine.dispose()
