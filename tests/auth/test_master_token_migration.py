from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.database import build_engine, immediate_transaction


def test_0016_migration_is_opt_in_singleton_and_refuses_destructive_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "master-migration.db"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "20260929_0015")
    engine = build_engine(url)
    try:
        assert "admin_master_identity" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0016")
    engine = build_engine(url)
    try:
        assert "admin_master_identity" in inspect(engine).get_table_names()
        with immediate_transaction(engine) as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            count = connection.execute(text("SELECT COUNT(*) FROM admin_master_identity"))
            assert count.scalar_one() == 0
            MasterTokenRepository(
                connection, identity_factory=lambda: "a" * 32
            ).initialize_from_local_cli("synthetic master token material 0001", now=1_000)
            verifier = connection.execute(
                text("SELECT token_verifier FROM admin_master_identity")
            ).scalar_one()
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO admin_master_identity "
                        "(slot, identity_id, token_verifier, session_generation, "
                        "created_at, updated_at) VALUES "
                        "(2, :identity_id, :token_verifier, 1, 1000, 1000)"
                    ),
                    {"identity_id": "b" * 32, "token_verifier": verifier},
                )
        with pytest.raises(RuntimeError, match="discard the administrator identity"):
            command.downgrade(config, "20260929_0015")
        with immediate_transaction(engine) as connection:
            connection.execute(text("DELETE FROM admin_master_identity"))
    finally:
        engine.dispose()
    command.downgrade(config, "20260929_0015")
    engine = build_engine(url)
    try:
        assert "admin_master_identity" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_0016_schema_matches_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "master-metadata.db"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)
