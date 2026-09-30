from __future__ import annotations

import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, select, text

import patchouli_lib.master_token_cli as master_token_cli
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.auth.models import MasterIdentity
from patchouli_lib.database import build_engine, immediate_transaction

_FIRST = "synthetic first administration token 0001"
_SECOND = "synthetic second administration token 0002"


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'master-token-cli.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _invoke(arguments: list[str], text: str) -> tuple[int, str, str]:
    output = StringIO()
    errors = StringIO()
    status = master_token_cli.main(
        arguments,
        stdin=StringIO(text),
        stdout=output,
        stderr=errors,
    )
    return status, output.getvalue(), errors.getvalue()


def test_initialize_and_rotate_invalidate_old_token_and_session_generation(
    migrated_database: Engine,
) -> None:
    assert _invoke(["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\n") == (0, "", "")
    with immediate_transaction(migrated_database) as connection:
        repository = MasterTokenRepository(connection)
        first = repository.authenticate(_FIRST)
        assert first is not None
        verifier = connection.execute(select(MasterIdentity.token_verifier)).scalar_one()
        assert _FIRST not in verifier

    assert _invoke(["rotate", "--stdin"], f"{_FIRST}\n{_SECOND}\n{_SECOND}\n") == (
        0,
        "",
        "",
    )
    with immediate_transaction(migrated_database) as connection:
        repository = MasterTokenRepository(connection)
        second = repository.authenticate(_SECOND)
        assert second is not None
        assert second.identity_id == first.identity_id
        assert second.session_generation == first.session_generation + 1
        assert repository.authenticate(_FIRST) is None
        assert not repository.is_session_generation_current(
            first.identity_id, first.session_generation
        )
        assert repository.is_session_generation_current(
            second.identity_id, second.session_generation
        )


def test_failed_reinit_or_rotation_never_changes_existing_token(migrated_database: Engine) -> None:
    assert _invoke(["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\n")[0] == 0
    repeated = _invoke(["initialize", "--stdin"], f"{_SECOND}\n{_SECOND}\n")
    wrong_old = _invoke(
        ["rotate", "--stdin"],
        f"incorrect existing administration token\n{_SECOND}\n{_SECOND}\n",
    )
    weak_new = _invoke(["rotate", "--stdin"], f"{_FIRST}\nshort\nshort\n")
    for status, output, errors in (repeated, wrong_old, weak_new):
        assert status != 0
        assert output == ""
        assert _FIRST not in errors
        assert _SECOND not in errors
        assert "incorrect" not in errors
    with immediate_transaction(migrated_database) as connection:
        repository = MasterTokenRepository(connection)
        assert repository.authenticate(_FIRST) is not None
        assert repository.authenticate(_SECOND) is None


def test_confirmed_local_recovery_without_old_token_invalidates_old_sessions(
    migrated_database: Engine,
) -> None:
    assert _invoke(["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\n") == (0, "", "")
    with migrated_database.connect() as connection:
        first = MasterTokenRepository(connection).authenticate(_FIRST)
        assert first is not None

    assert _invoke(["recover", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n{_SECOND}\n") == (
        0,
        "",
        "",
    )

    with migrated_database.connect() as connection:
        repository = MasterTokenRepository(connection)
        recovered = repository.authenticate(_SECOND)
        assert recovered is not None
        assert recovered.identity_id == first.identity_id
        assert recovered.session_generation == first.session_generation + 1
        assert repository.authenticate(_FIRST) is None
        assert not repository.is_session_generation_current(
            first.identity_id, first.session_generation
        )


@pytest.mark.parametrize("new_token", [_FIRST, "short", "x" * 1_025])
def test_failed_recovery_keeps_existing_token_and_generation(
    migrated_database: Engine, new_token: str
) -> None:
    assert _invoke(["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\n")[0] == 0
    with migrated_database.connect() as connection:
        first = MasterTokenRepository(connection).authenticate(_FIRST)

    status, output, errors = _invoke(
        ["recover", "--confirm-local-reset", "--stdin"], f"{new_token}\n{new_token}\n"
    )

    assert status != 0
    assert output == ""
    assert new_token not in errors
    assert _FIRST not in errors
    with migrated_database.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(_FIRST) == first


def test_local_recovery_does_not_create_master_identity(migrated_database: Engine) -> None:
    assert _invoke(["recover", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n{_SECOND}\n") == (
        1,
        "",
        "Master token operation failed. No token was printed.\n",
    )
    with migrated_database.connect() as connection:
        assert not MasterTokenRepository(connection).has_identity()


@pytest.mark.parametrize(
    ("arguments", "text"),
    [
        (["initialize", _FIRST], ""),
        (["initialize"], f"{_FIRST}\n{_FIRST}\n"),
        (["initialize", "--stdin"], f"{_FIRST}\n{_SECOND}\n"),
        (["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\nextra"),
        (["rotate", "--stdin"], f"{_FIRST}\n{_SECOND}\n"),
        (["initialize", "--stdin"], "x" * 1_026),
        (["recover"], f"{_SECOND}\n{_SECOND}\n"),
        (["recover", "--stdin"], f"{_SECOND}\n{_SECOND}\n"),
        (["recover", "--confirm-local-reset", _SECOND], ""),
        (["recover", "--confirm-local-reset", "--stdin", _SECOND], ""),
        (["recover", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n"),
        (["recover", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n{_FIRST}\n"),
        (["recover", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n{_SECOND}\nextra"),
        (["initialize", "--confirm-local-reset", "--stdin"], f"{_SECOND}\n{_SECOND}\n"),
    ],
)
def test_bad_command_and_input_are_redacted(
    migrated_database: Engine,
    arguments: list[str],
    text: str,
) -> None:
    status, output, errors = _invoke(arguments, text)
    assert status == 2
    assert output == ""
    assert errors == "Invalid master token command or input.\n"
    assert _FIRST not in errors
    with immediate_transaction(migrated_database) as connection:
        assert not MasterTokenRepository(connection).has_identity()


@pytest.mark.parametrize("command", ["initialize", "recover"])
def test_unmigrated_database_fails_closed_without_creating_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'unmigrated.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    arguments = [command, "--stdin"]
    if command == "recover":
        arguments.insert(1, "--confirm-local-reset")
    status, output, errors = _invoke(arguments, f"{_FIRST}\n{_FIRST}\n")
    assert status == 1
    assert output == ""
    assert errors == "Master token operation failed. No token was printed.\n"
    assert _FIRST not in errors
    assert not (tmp_path / "unmigrated.db").exists()


@pytest.mark.parametrize("command", ["initialize", "recover"])
def test_existing_unmigrated_database_is_not_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'old-schema.db').as_posix()}"
    engine = build_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260929_0015')"))
    engine.dispose()
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")

    arguments = [command, "--stdin"]
    if command == "recover":
        arguments.insert(1, "--confirm-local-reset")
    status, output, errors = _invoke(arguments, f"{_FIRST}\n{_FIRST}\n")

    assert status == 1
    assert output == ""
    assert errors == "Master token operation failed. No token was printed.\n"
    engine = build_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260929_0015"
        )
        assert (
            connection.execute(
                text("SELECT name FROM sqlite_schema WHERE name = 'admin_master_identity'")
            ).scalar_one_or_none()
            is None
        )
    engine.dispose()


class _InteractiveInput(StringIO):
    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        del size
        raise AssertionError("Interactive input must not be read after getpass.")


@pytest.mark.parametrize("command", ["initialize", "recover"])
def test_interactive_mode_uses_hidden_input_and_does_not_echo(
    migrated_database: Engine, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    if command == "recover":
        assert _invoke(["initialize", "--stdin"], f"{_FIRST}\n{_FIRST}\n")[0] == 0
    presented = iter((_SECOND, _SECOND))
    prompts: list[str] = []

    def hidden_input(prompt: str, *, stream: StringIO) -> str:
        assert stream is errors
        prompts.append(prompt)
        return next(presented)

    monkeypatch.setattr(sys, "stdin", _InteractiveInput())
    monkeypatch.setattr(master_token_cli, "getpass", hidden_input)
    output = StringIO()
    errors = StringIO()
    arguments = [command] if command == "initialize" else [command, "--confirm-local-reset"]
    assert master_token_cli.main(arguments, stdout=output, stderr=errors) == 0
    assert prompts == ["New master token: ", "Confirm new master token: "]
    assert output.getvalue() == errors.getvalue() == ""
    assert _SECOND not in output.getvalue()
    with immediate_transaction(migrated_database) as connection:
        assert MasterTokenRepository(connection).authenticate(_SECOND) is not None
