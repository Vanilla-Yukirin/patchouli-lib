"""The rebuild operation is explicit and never runs on import/startup."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from patchouli_lib.database import build_engine
from patchouli_lib.search_cli import main


def test_rebuild_cli_is_explicit_and_activates_empty_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'search.sqlite').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        with pytest.raises(SystemExit):
            main([])
        with engine.connect() as connection:
            assert connection.execute(text("SELECT ready FROM search_meta")).scalar_one() == 0
        assert main(["rebuild"]) == 0
        assert "ready" in capsys.readouterr().out
        with engine.connect() as connection:
            assert connection.execute(text("SELECT ready FROM search_meta")).scalar_one() == 1
    finally:
        engine.dispose()
