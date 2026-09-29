from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.models import AgentTokenValue, Credential
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewCredential
from patchouli_lib.auth.service import (
    AuthenticationService,
    CredentialIssuer,
    CredentialPersistenceError,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import build_engine, immediate_transaction

LIBRARY = "1" * 32
AGENT = "4" * 32
OPERATOR = "5" * 32


def _caller(repository: AuthRepository, *, kind: CallerKind, caller_id: str) -> None:
    repository.add_caller(
        NewCaller(
            id=caller_id,
            library_id=LIBRARY,
            kind=kind,
            name=f"Synthetic {kind.value} {caller_id[0]}",
            created_at=2_000_000,
            updated_at=2_000_000,
        )
    )


def test_only_new_agent_values_are_revealable_and_auth_queries_stay_verifier_only(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    assert scoped_library[0] == LIBRARY
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _caller(repository, kind=CallerKind.AGENT, caller_id=AGENT)
        _caller(repository, kind=CallerKind.OPERATOR, caller_id=OPERATOR)
        agent = repository.get_caller(LIBRARY, AGENT)
        operator = repository.get_caller(LIBRARY, OPERATOR)
        assert agent is not None and operator is not None
        legacy = generate_token()
        repository.add_credential(
            NewCredential(
                id="6" * 32,
                library_id=LIBRARY,
                caller_id=AGENT,
                selector=legacy.selector,
                token_version=legacy.version,
                verifier=legacy.verifier,
                expires_at=10_000_000,
                created_at=2_000_000,
                updated_at=2_000_000,
            )
        )
        issued_agent = CredentialIssuer(
            repository, id_factory=lambda: "7" * 32, clock=lambda: 3_000_000
        ).issue(agent, expires_at=10_000_000)
        issued_operator = CredentialIssuer(
            repository, id_factory=lambda: "8" * 32, clock=lambda: 3_000_000
        ).issue(operator, expires_at=10_000_000)
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, issued_agent.credential.id, active_at=4_000_000
            )
            == issued_agent.value
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, issued_agent.credential.id, active_at=10_000_000
            )
            is None
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, OPERATOR, issued_agent.credential.id, active_at=4_000_000
            )
            is None
        )
        assert (
            repository.get_active_agent_token_value(LIBRARY, AGENT, "6" * 32, active_at=4_000_000)
            is None
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, OPERATOR, issued_operator.credential.id, active_at=4_000_000
            )
            is None
        )
        assert (
            AuthenticationService(repository, clock=lambda: 4_000_000)
            .authenticate(issued_agent.value)
            .caller.id
            == AGENT
        )
        stored = repository.find_credential_by_selector(issued_agent.value.split(".")[1])
        assert stored is not None
        assert issued_agent.value not in repr(stored)
        assert "token_value" not in type(stored).model_fields
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1
        connection.execute(
            update(Credential)
            .where(Credential.id == issued_agent.credential.id)
            .values(verifier=b"\x00" * 32)
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, issued_agent.credential.id, active_at=4_000_000
            )
            is None
        )


def test_failed_agent_value_insert_leaves_no_verifier_after_outer_commit(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    assert scoped_library[0] == LIBRARY
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _caller(repository, kind=CallerKind.AGENT, caller_id=AGENT)
        connection.exec_driver_sql(
            "CREATE TRIGGER synthetic_agent_value_failure "
            "BEFORE INSERT ON auth_agent_token_values "
            "BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END"
        )
        caller = repository.get_caller(LIBRARY, AGENT)
        assert caller is not None
        with pytest.raises(CredentialPersistenceError) as exc_info:
            CredentialIssuer(
                repository, id_factory=lambda: "7" * 32, clock=lambda: 3_000_000
            ).issue(caller, expires_at=10_000_000)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert connection.scalar(select(func.count()).select_from(Credential)) == 0
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 0
    with auth_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Credential)) == 0


def test_revoke_rotate_and_disable_remove_revealable_values(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    assert scoped_library[0] == LIBRARY
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        _caller(repository, kind=CallerKind.AGENT, caller_id=AGENT)
        caller = repository.get_caller(LIBRARY, AGENT)
        assert caller is not None
        ids = iter(("7" * 32, "8" * 32, "9" * 32, "a" * 32))
        issuer = CredentialIssuer(repository, id_factory=lambda: next(ids), clock=lambda: 3_000_000)
        first = issuer.issue(caller, expires_at=10_000_000)
        second = issuer.issue(caller, expires_at=10_000_000)
        replacement = issuer.issue(caller, expires_at=10_000_000)
        fourth = issuer.issue(caller, expires_at=10_000_000)
        first_stored = repository.get_credential(LIBRARY, AGENT, first.credential.id)
        second_stored = repository.get_credential(LIBRARY, AGENT, second.credential.id)
        assert first_stored is not None and second_stored is not None
        repository.revoke_credential(first_stored, revoked_at=4_000_000)
        repository.mark_credential_rotated(
            second_stored, replacement.credential.id, rotated_at=4_000_000
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, first.credential.id, active_at=4_000_000
            )
            is None
        )
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, second.credential.id, active_at=4_000_000
            )
            is None
        )
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 2
        repository.disable_caller(LIBRARY, AGENT, disabled_at=5_000_000)
        assert (
            repository.get_active_agent_token_value(
                LIBRARY, AGENT, fourth.credential.id, active_at=5_000_000
            )
            is None
        )
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 0


def test_0015_migration_keeps_legacy_tokens_unrecoverable_and_guards_direct_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "agent-values.db"
    url = f"sqlite:///{database.as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "20260929_0014")
    engine = build_engine(url)
    try:
        with immediate_transaction(engine) as connection:
            connection.execute(
                text(
                    "INSERT INTO libraries (id, name, created_at, updated_at) "
                    "VALUES (:id, 'Synthetic Library', 1000000, 1000000)"
                ),
                {"id": LIBRARY},
            )
            connection.execute(
                text(
                    "INSERT INTO sections "
                    "(id, library_id, name, description, created_at, updated_at) "
                    "VALUES (:id, :library_id, 'Synthetic Section', '', 1000000, 1000000)"
                ),
                {"id": "2" * 32, "library_id": LIBRARY},
            )
            connection.execute(
                text(
                    "INSERT INTO books "
                    "(id, library_id, section_id, name, summary, created_at, updated_at) "
                    "VALUES (:id, :library_id, :section_id, 'Synthetic Book', '', 1000000, 1000000)"
                ),
                {"id": "3" * 32, "library_id": LIBRARY, "section_id": "2" * 32},
            )
            repository = AuthRepository(connection)
            _caller(repository, kind=CallerKind.AGENT, caller_id=AGENT)
            _caller(repository, kind=CallerKind.OPERATOR, caller_id=OPERATOR)
            legacy = generate_token()
            repository.add_credential(
                NewCredential(
                    id="6" * 32,
                    library_id=LIBRARY,
                    caller_id=AGENT,
                    selector=legacy.selector,
                    token_version=legacy.version,
                    verifier=legacy.verifier,
                    expires_at=10_000_000,
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
    finally:
        engine.dispose()
    command.upgrade(config, "20260929_0015")
    engine = build_engine(url)
    try:
        with immediate_transaction(engine) as connection:
            repository = AuthRepository(connection)
            assert (
                repository.get_active_agent_token_value(
                    LIBRARY, AGENT, "6" * 32, active_at=3_000_000
                )
                is None
            )
            operator = repository.get_caller(LIBRARY, OPERATOR)
            agent = repository.get_caller(LIBRARY, AGENT)
            assert operator is not None and agent is not None
            operator_issued = CredentialIssuer(
                repository, id_factory=lambda: "7" * 32, clock=lambda: 3_000_000
            ).issue(operator, expires_at=10_000_000)
            with pytest.raises(IntegrityError):
                connection.execute(
                    insert(AgentTokenValue),
                    {"credential_id": operator_issued.credential.id, "token_value": legacy.value},
                )
            agent_issued = CredentialIssuer(
                repository, id_factory=lambda: "8" * 32, clock=lambda: 3_000_000
            ).issue(agent, expires_at=10_000_000)
            assert (
                repository.get_active_agent_token_value(
                    LIBRARY, AGENT, agent_issued.credential.id, active_at=3_000_000
                )
                == agent_issued.value
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    insert(AgentTokenValue),
                    {"credential_id": "6" * 32, "token_value": agent_issued.value},
                )
        with pytest.raises(RuntimeError, match="discard revealable Agent token values"):
            command.downgrade(config, "20260929_0014")
        with immediate_transaction(engine) as connection:
            connection.exec_driver_sql(
                "UPDATE auth_credentials SET revoked_at = 4000000, updated_at = 4000000 "
                "WHERE id = ?",
                ("8" * 32,),
            )
            assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 0
    finally:
        engine.dispose()
    command.downgrade(config, "20260929_0014")
