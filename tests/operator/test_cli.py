from __future__ import annotations

from collections.abc import Iterator, Sequence
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from patchouli_lib import operator_cli
from patchouli_lib.auth.models import (
    AuditEvent,
    BootstrapMarker,
    Caller,
    Credential,
    SectionGrant,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
)
from patchouli_lib.database import build_engine
from patchouli_lib.library.models import Book, Library, Section
from patchouli_lib.operator.service import OperatorService

_LIBRARY_NAME = "Synthetic Local Library"
_SECTION_NAME = "Synthetic Agent Section"
_BOOK_NAME = "Synthetic Archive Book"


class _BrokenOutput(StringIO):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self._failure = failure
        self.attempted_value = ""

    def write(self, value: str) -> int:
        self.attempted_value = value
        if self._failure == "write":
            super().write(value[: len(value) // 2])
            raise OSError(f"synthetic output failure: {value}")
        return super().write(value)

    def flush(self) -> None:
        if self._failure == "flush":
            raise OSError(f"synthetic flush failure: {self.attempted_value}")
        super().flush()


class _ShortOutput(StringIO):
    def __init__(self, *, seekable: bool) -> None:
        super().__init__()
        self._is_seekable = seekable
        self.attempted_value = ""

    def write(self, value: str) -> int:
        self.attempted_value = value
        prefix = value[: len(value) // 2]
        super().write(prefix)
        return len(prefix)

    def seekable(self) -> bool:
        return self._is_seekable


@pytest.fixture
def cli_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Engine, list[int]]]:
    database_path = tmp_path / "operator-cli.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = build_engine(database_url)
    Caller.metadata.create_all(engine)
    clock = [1_000_000]
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    monkeypatch.setattr(operator_cli, "utc_microseconds", lambda: clock[0])
    try:
        yield database_path, engine, clock
    finally:
        engine.dispose()


def _run(
    argv: Sequence[str],
    *,
    stdin_text: str = "",
    stdout_stream: StringIO | None = None,
) -> tuple[int, str, str]:
    stdout = StringIO() if stdout_stream is None else stdout_stream
    stderr = StringIO()
    exit_code = operator_cli.main(
        argv,
        stdin=StringIO(stdin_text),
        stdout=stdout,
        stderr=stderr,
    )
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _bootstrap_arguments() -> list[str]:
    return [
        "bootstrap",
        "--library-name",
        _LIBRARY_NAME,
        "--section-name",
        _SECTION_NAME,
        "--section-description",
        "Synthetic policy boundary",
        "--book-name",
        _BOOK_NAME,
        "--book-summary",
        "Synthetic archive destination",
        "--operator-name",
        "Synthetic Local Operator",
        "--operator-description",
        "Local-only test identity",
        "--credential-ttl-seconds",
        "60",
    ]


def _provision_arguments() -> list[str]:
    return [
        "provision-agent",
        "--library-name",
        _LIBRARY_NAME,
        "--section-name",
        _SECTION_NAME,
        "--agent-name",
        "Synthetic Archive Agent",
        "--agent-description",
        "Synthetic scoped caller",
        "--credential-ttl-seconds",
        "120",
        "--grant",
        SectionAction.QUERY.value,
        "--grant",
        SectionAction.ARCHIVE_WRITE.value,
    ]


def _revoke_arguments(caller_id: str, credential_id: str) -> list[str]:
    return [
        "revoke-agent-credential",
        "--library-name",
        _LIBRARY_NAME,
        "--caller-id",
        caller_id,
        "--credential-id",
        credential_id,
    ]


def _token(stdout: str) -> str:
    assert stdout.endswith("\n")
    value = stdout.removesuffix("\n")
    assert value.startswith("plb1.")
    assert "\n" not in value
    return value


def _prepare_agent_credential(
    engine: Engine,
    clock: list[int],
) -> tuple[str, str, str, str, str, str]:
    bootstrap_code, bootstrap_stdout, bootstrap_stderr = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    assert bootstrap_stderr == ""
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    provision_code, provision_stdout, provision_stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
    )
    assert provision_code == 0
    assert provision_stderr == ""
    agent_token = _token(provision_stdout)
    with engine.connect() as connection:
        operator = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.OPERATOR.value)
            )
            .mappings()
            .one()
        )
        agent = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.AGENT.value)
            )
            .mappings()
            .one()
        )
        operator_credential_id = connection.scalar(
            select(Credential.id).where(Credential.caller_id == operator.id)
        )
        agent_credential_id = connection.scalar(
            select(Credential.id).where(Credential.caller_id == agent.id)
        )
    assert isinstance(operator_credential_id, str)
    assert isinstance(agent_credential_id, str)
    return (
        operator_token,
        agent_token,
        operator.id,
        operator_credential_id,
        agent.id,
        agent_credential_id,
    )


def test_bootstrap_seeds_structure_and_outputs_secret_only_after_commit(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    database_path, engine, _ = cli_database

    exit_code, stdout, stderr = _run(_bootstrap_arguments())

    assert exit_code == 0
    assert stderr == ""
    operator_token = _token(stdout)
    with engine.connect() as connection:
        library = connection.execute(select(Library.__table__)).mappings().one()
        section = connection.execute(select(Section.__table__)).mappings().one()
        book = connection.execute(select(Book.__table__)).mappings().one()
        caller = connection.execute(select(Caller.__table__)).mappings().one()
        credential = connection.execute(select(Credential.__table__)).mappings().one()
        marker = connection.execute(select(BootstrapMarker.__table__)).mappings().one()
        audit = connection.execute(select(AuditEvent.__table__)).mappings().one()

        assert library.name == _LIBRARY_NAME
        assert section.library_id == library.id
        assert section.name == _SECTION_NAME
        assert book.section_id == section.id
        assert book.name == _BOOK_NAME
        assert caller.kind == CallerKind.OPERATOR.value
        assert credential.caller_id == caller.id
        assert credential.expires_at == 61_000_000
        assert marker.operator_caller_id == caller.id
        assert audit.action == "operator.bootstrap"
        assert connection.scalar(select(func.count()).select_from(SectionGrant)) == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert operator_token.encode() not in database_path.read_bytes()

    repeated_code, repeated_stdout, repeated_stderr = _run(_bootstrap_arguments())
    assert repeated_code == 1
    assert repeated_stdout == ""
    assert repeated_stderr == "Operator command failed.\n"
    assert operator_token not in repeated_stderr
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Library)) == 1
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_local_recovery_retires_prior_operator_token_without_replaying_it(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    database_path, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    prior_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000

    exit_code, stdout, stderr = _run(
        [
            "recover",
            "--library-name",
            _LIBRARY_NAME,
            "--credential-ttl-seconds",
            "90",
        ]
    )

    assert exit_code == 0
    assert stderr == ""
    recovered_token = _token(stdout)
    assert recovered_token != prior_token
    assert prior_token not in stdout
    with engine.connect() as connection:
        credentials = (
            connection.execute(select(Credential.__table__).order_by(Credential.created_at))
            .mappings()
            .all()
        )
        audits = connection.execute(
            select(AuditEvent.action).order_by(AuditEvent.occurred_at)
        ).scalars()
        assert len(credentials) == 2
        assert credentials[0].revoked_at == 2_000_000
        assert credentials[1].revoked_at is None
        assert credentials[1].expires_at == 92_000_000
        assert list(audits) == [
            "operator.bootstrap",
            "auth.operator.recovery",
        ]
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
    database_bytes = database_path.read_bytes()
    assert prior_token.encode() not in database_bytes
    assert recovered_token.encode() not in database_bytes


def test_provision_agent_reads_operator_token_from_stdin_and_grants_exact_actions(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    database_path, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    arguments = [*_provision_arguments(), "--grant", SectionAction.QUERY.value]

    exit_code, stdout, stderr = _run(arguments, stdin_text=f"{operator_token}\r\n")

    assert exit_code == 0
    assert stderr == ""
    agent_token = _token(stdout)
    assert operator_token not in stdout
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        library = connection.execute(select(Library.__table__)).mappings().one()
        section = connection.execute(select(Section.__table__)).mappings().one()
        agent = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.AGENT.value)
            )
            .mappings()
            .one()
        )
        grants = (
            connection.execute(
                select(SectionGrant.__table__).where(SectionGrant.caller_id == agent.id)
            )
            .mappings()
            .all()
        )
        agent_credential = (
            connection.execute(select(Credential.__table__).where(Credential.caller_id == agent.id))
            .mappings()
            .one()
        )
        audit_actions = (
            connection.execute(
                select(AuditEvent.action).order_by(AuditEvent.occurred_at, AuditEvent.action)
            )
            .scalars()
            .all()
        )
        assert agent.name == "Synthetic Archive Agent"
        assert agent_credential.expires_at == 122_000_000
        assert {(grant.section_id, grant.action) for grant in grants} == {
            (section.id, SectionAction.QUERY.value),
            (section.id, SectionAction.ARCHIVE_WRITE.value),
        }
        assert audit_actions.count("auth.caller.create") == 1
        assert audit_actions.count("auth.credential.create") == 1
        assert audit_actions.count("auth.grant.add") == 2

        authentication = AuthenticationService(repository, clock=lambda: 3_000_000)
        for action in (SectionAction.QUERY, SectionAction.ARCHIVE_WRITE):
            authenticated = authentication.authorize_content(
                agent_token,
                library_id=library.id,
                section_id=section.id,
                action=action,
            )
            assert authenticated.caller.id == agent.id
        with pytest.raises(AuthorizationError):
            authentication.authorize_content(
                agent_token,
                library_id=library.id,
                section_id=section.id,
                action=SectionAction.PAGE_READ,
            )
        connection.rollback()
    database_bytes = database_path.read_bytes()
    assert operator_token.encode() not in database_bytes
    assert agent_token.encode() not in database_bytes


def test_revoke_agent_credential_commits_exact_audit_and_rejects_bearer(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    database_path, engine, clock = cli_database
    (
        operator_token,
        agent_token,
        operator_id,
        operator_credential_id,
        agent_id,
        agent_credential_id,
    ) = _prepare_agent_credential(engine, clock)
    arguments = _revoke_arguments(agent_id, agent_credential_id)
    assert operator_token not in arguments
    assert agent_token not in arguments
    clock[0] = 3_000_000

    exit_code, stdout, stderr = _run(
        arguments,
        stdin_text=f"{operator_token}\r\n",
    )

    assert exit_code == 0
    assert stdout == ""
    assert stderr == ""
    with engine.connect() as connection:
        revoked_at = connection.scalar(
            select(Credential.revoked_at).where(Credential.id == agent_credential_id)
        )
        audit = (
            connection.execute(
                select(AuditEvent.__table__).where(AuditEvent.action == "auth.credential.revoke")
            )
            .mappings()
            .one()
        )
        assert revoked_at == 3_000_000
        assert audit.actor_caller_id == operator_id
        assert audit.actor_credential_id == operator_credential_id
        assert audit.target_caller_id is None
        assert audit.action == "auth.credential.revoke"
        assert audit.resource_type == "credential"
        assert audit.resource_id == agent_credential_id
        assert audit.outcome == "succeeded"
        assert audit.request_id.startswith("req_local_")
        assert audit.occurred_at == 3_000_000
        with pytest.raises(AuthenticationError):
            AuthenticationService(
                AuthRepository(connection),
                clock=lambda: 4_000_000,
            ).authenticate(agent_token)
        connection.rollback()
    database_bytes = database_path.read_bytes()
    assert operator_token.encode() not in database_bytes
    assert agent_token.encode() not in database_bytes

    clock[0] = 4_000_000
    repeated_code, repeated_stdout, repeated_stderr = _run(
        arguments,
        stdin_text=f"{operator_token}\n",
    )
    assert repeated_code == 0
    assert repeated_stdout == ""
    assert repeated_stderr == ""
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(Credential.revoked_at).where(Credential.id == agent_credential_id)
            )
            == 3_000_000
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "auth.credential.revoke")
            )
            == 1
        )


def test_revoke_agent_credential_rejects_malformed_stdin_without_state_drift(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, clock = cli_database
    (
        operator_token,
        _,
        _,
        _,
        agent_id,
        agent_credential_id,
    ) = _prepare_agent_credential(engine, clock)
    with engine.connect() as connection:
        credentials_before = [
            dict(row)
            for row in connection.execute(
                select(Credential.__table__).order_by(Credential.id)
            ).mappings()
        ]
        audits_before = [
            dict(row)
            for row in connection.execute(
                select(AuditEvent.__table__).order_by(AuditEvent.id)
            ).mappings()
        ]
    clock[0] = 3_000_000

    exit_code, stdout, stderr = _run(
        _revoke_arguments(agent_id, agent_credential_id),
        stdin_text=f"{operator_token}\nunexpected-second-line\n",
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr == "Invalid operator command input.\n"
    assert operator_token not in stderr
    with engine.connect() as connection:
        credentials_after = [
            dict(row)
            for row in connection.execute(
                select(Credential.__table__).order_by(Credential.id)
            ).mappings()
        ]
        audits_after = [
            dict(row)
            for row in connection.execute(
                select(AuditEvent.__table__).order_by(AuditEvent.id)
            ).mappings()
        ]
    assert credentials_after == credentials_before
    assert audits_after == audits_before


def test_revoke_agent_credential_hides_wrong_targets_and_operator_credential(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, clock = cli_database
    (
        operator_token,
        agent_token,
        operator_id,
        operator_credential_id,
        agent_id,
        agent_credential_id,
    ) = _prepare_agent_credential(engine, clock)
    with engine.connect() as connection:
        callers_before = [
            dict(row)
            for row in connection.execute(select(Caller.__table__).order_by(Caller.id)).mappings()
        ]
        credentials_before = [
            dict(row)
            for row in connection.execute(
                select(Credential.__table__).order_by(Credential.id)
            ).mappings()
        ]
        audits_before = [
            dict(row)
            for row in connection.execute(
                select(AuditEvent.__table__).order_by(AuditEvent.id)
            ).mappings()
        ]
    bad_arguments = [
        _revoke_arguments("f" * 32, agent_credential_id),
        _revoke_arguments(agent_id, "e" * 32),
        _revoke_arguments(operator_id, operator_credential_id),
        [
            "revoke-agent-credential",
            "--library-name",
            "Missing Synthetic Library",
            "--caller-id",
            agent_id,
            "--credential-id",
            agent_credential_id,
        ],
    ]
    clock[0] = 3_000_000

    for arguments in bad_arguments:
        assert operator_token not in arguments
        assert agent_token not in arguments
        exit_code, stdout, stderr = _run(
            arguments,
            stdin_text=f"{operator_token}\n",
        )
        assert exit_code == 1
        assert stdout == ""
        assert stderr == "Operator command failed.\n"
        assert operator_token not in stderr
        assert agent_token not in stderr

    with engine.connect() as connection:
        callers_after = [
            dict(row)
            for row in connection.execute(select(Caller.__table__).order_by(Caller.id)).mappings()
        ]
        credentials_after = [
            dict(row)
            for row in connection.execute(
                select(Credential.__table__).order_by(Credential.id)
            ).mappings()
        ]
        audits_after = [
            dict(row)
            for row in connection.execute(
                select(AuditEvent.__table__).order_by(AuditEvent.id)
            ).mappings()
        ]
    assert callers_after == callers_before
    assert credentials_after == credentials_before
    assert audits_after == audits_before


def test_provision_failure_rolls_back_identity_credential_grants_and_audit(
    cli_database: tuple[Path, Engine, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000

    def fail_grant(self: OperatorService, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise RuntimeError("synthetic rollback sentinel")

    monkeypatch.setattr(OperatorService, "add_grant", fail_grant)
    exit_code, stdout, stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
    )

    assert exit_code == 1
    assert stdout == ""
    assert stderr == "Operator command failed.\n"
    assert operator_token not in stderr
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count()).select_from(Caller).where(Caller.kind == "agent")
            )
            == 0
        )
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(SectionGrant)) == 0
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_agent_output_failure_revokes_unknown_credential_and_audits_compensation(
    cli_database: tuple[Path, Engine, list[int]],
    failure: str,
) -> None:
    database_path, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    broken_output = _BrokenOutput(failure)

    exit_code, stdout, stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
        stdout_stream=broken_output,
    )

    attempted_token = _token(broken_output.attempted_value)
    assert exit_code == 1
    assert stdout == ""
    assert stderr == "Agent credential output failed. The credential was revoked.\n"
    assert attempted_token not in stdout
    assert attempted_token not in stderr
    with engine.connect() as connection:
        agent = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.AGENT.value)
            )
            .mappings()
            .one()
        )
        credential = (
            connection.execute(select(Credential.__table__).where(Credential.caller_id == agent.id))
            .mappings()
            .one()
        )
        actions = connection.execute(select(AuditEvent.action)).scalars().all()
        assert credential.revoked_at == 2_000_000
        assert actions.count("auth.caller.create") == 1
        assert actions.count("auth.credential.create") == 1
        assert actions.count("auth.grant.add") == 2
        assert actions.count("auth.credential.revoke") == 1
        with pytest.raises(AuthenticationError, match="Invalid or inactive credential"):
            AuthenticationService(
                AuthRepository(connection),
                clock=lambda: 3_000_000,
            ).authenticate(attempted_token)
    assert attempted_token.encode() not in database_path.read_bytes()


@pytest.mark.parametrize("seekable", [True, False])
def test_agent_short_write_is_failed_delivery_with_revoked_credential(
    cli_database: tuple[Path, Engine, list[int]],
    seekable: bool,
) -> None:
    database_path, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    short_output = _ShortOutput(seekable=seekable)

    exit_code, stdout, stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
        stdout_stream=short_output,
    )

    attempted_token = _token(short_output.attempted_value)
    assert exit_code == 1
    assert stdout == ("" if seekable else short_output.attempted_value[: len(stdout)])
    assert stderr == "Agent credential output failed. The credential was revoked.\n"
    assert attempted_token not in stderr
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        agent = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.AGENT.value)
            )
            .mappings()
            .one()
        )
        credential = (
            connection.execute(select(Credential.__table__).where(Credential.caller_id == agent.id))
            .mappings()
            .one()
        )
        actions = connection.execute(select(AuditEvent.action)).scalars().all()
        assert credential.revoked_at == 2_000_000
        assert actions.count("auth.caller.create") == 1
        assert actions.count("auth.credential.create") == 1
        assert actions.count("auth.grant.add") == 2
        assert actions.count("auth.credential.revoke") == 1
        with pytest.raises(AuthenticationError, match="Invalid or inactive credential"):
            AuthenticationService(repository, clock=lambda: 3_000_000).authenticate(attempted_token)
    assert attempted_token.encode() not in database_path.read_bytes()


def test_short_write_without_integer_count_is_failed_local_delivery(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, _ = cli_database

    class NoneReturningOutput(StringIO):
        def write(self, value: str) -> int:
            super().write(value[: len(value) // 2])
            return None  # type: ignore[return-value]

    output = NoneReturningOutput()

    exit_code, stdout, stderr = _run(_bootstrap_arguments(), stdout_stream=output)

    assert exit_code == 1
    assert stdout == ""
    assert stderr == (
        "Credential output failed. Run local operator recovery again before continuing.\n"
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1


def test_agent_compensation_failure_returns_fixed_unconfirmed_status(
    cli_database: tuple[Path, Engine, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    broken_output = _BrokenOutput("flush")

    def failed_compensation(
        _: Engine,
        compensation: operator_cli._AgentCompensation,
    ) -> bool:
        assert operator_token not in repr(compensation)
        return False

    monkeypatch.setattr(operator_cli, "_revoke_failed_agent_delivery", failed_compensation)

    exit_code, stdout, stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
        stdout_stream=broken_output,
    )

    attempted_token = _token(broken_output.attempted_value)
    assert exit_code == 1
    assert stdout == ""
    assert stderr == ("Agent credential output failed. Credential status could not be confirmed.\n")
    assert operator_token not in stderr
    assert attempted_token not in stderr
    with engine.connect() as connection:
        agent = (
            connection.execute(
                select(Caller.__table__).where(Caller.kind == CallerKind.AGENT.value)
            )
            .mappings()
            .one()
        )
        credential = (
            connection.execute(select(Credential.__table__).where(Credential.caller_id == agent.id))
            .mappings()
            .one()
        )
        actions = connection.execute(select(AuditEvent.action)).scalars().all()
        assert credential.revoked_at is None
        assert actions.count("auth.credential.revoke") == 0


def test_bootstrap_output_failure_uses_fixed_local_recovery_instruction(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    database_path, engine, clock = cli_database
    broken_output = _BrokenOutput("write")

    exit_code, stdout, stderr = _run(
        _bootstrap_arguments(),
        stdout_stream=broken_output,
    )

    lost_token = _token(broken_output.attempted_value)
    assert exit_code == 1
    assert stdout == ""
    assert stderr == (
        "Credential output failed. Run local operator recovery again before continuing.\n"
    )
    assert lost_token not in stderr
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BootstrapMarker)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
    assert lost_token.encode() not in database_path.read_bytes()

    clock[0] = 2_000_000
    recovery_code, recovery_stdout, recovery_stderr = _run(
        [
            "recover",
            "--library-name",
            _LIBRARY_NAME,
            "--credential-ttl-seconds",
            "90",
        ]
    )
    assert recovery_code == 0
    assert recovery_stderr == ""
    assert _token(recovery_stdout) != lost_token


def test_recovery_output_failure_requires_another_recovery_to_retire_lost_token(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, clock = cli_database
    bootstrap_code, _, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    clock[0] = 2_000_000
    broken_output = _BrokenOutput("flush")
    recover_arguments = [
        "recover",
        "--library-name",
        _LIBRARY_NAME,
        "--credential-ttl-seconds",
        "90",
    ]

    exit_code, stdout, stderr = _run(
        recover_arguments,
        stdout_stream=broken_output,
    )

    lost_token = _token(broken_output.attempted_value)
    assert exit_code == 1
    assert stdout == ""
    assert stderr == (
        "Credential output failed. Run local operator recovery again before continuing.\n"
    )
    clock[0] = 3_000_000
    retry_code, retry_stdout, retry_stderr = _run(recover_arguments)
    assert retry_code == 0
    assert retry_stderr == ""
    assert _token(retry_stdout) != lost_token
    with engine.connect() as connection:
        credentials = (
            connection.execute(select(Credential.__table__).order_by(Credential.created_at))
            .mappings()
            .all()
        )
        assert len(credentials) == 3
        assert credentials[0].revoked_at == 2_000_000
        assert credentials[1].revoked_at == 3_000_000
        assert credentials[2].revoked_at is None


def test_malformed_multiline_stdin_fails_without_mutation_or_audit(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000

    exit_code, stdout, stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\nunexpected-second-line\n",
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr == "Invalid operator command input.\n"
    assert operator_token not in stderr
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Caller)) == 1
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(SectionGrant)) == 0
        assert connection.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_duplicate_agent_provision_rolls_back_without_extra_audit(
    cli_database: tuple[Path, Engine, list[int]],
) -> None:
    _, engine, clock = cli_database
    bootstrap_code, bootstrap_stdout, _ = _run(_bootstrap_arguments())
    assert bootstrap_code == 0
    operator_token = _token(bootstrap_stdout)
    clock[0] = 2_000_000
    first_code, _, first_stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
    )
    assert first_code == 0
    assert first_stderr == ""
    with engine.connect() as connection:
        counts_before = tuple(
            connection.scalar(select(func.count()).select_from(model))
            for model in (Caller, Credential, SectionGrant, AuditEvent)
        )

    repeated_code, repeated_stdout, repeated_stderr = _run(
        _provision_arguments(),
        stdin_text=f"{operator_token}\n",
    )

    assert repeated_code == 1
    assert repeated_stdout == ""
    assert repeated_stderr == "Operator command failed.\n"
    with engine.connect() as connection:
        counts_after = tuple(
            connection.scalar(select(func.count()).select_from(model))
            for model in (Caller, Credential, SectionGrant, AuditEvent)
        )
    assert counts_after == counts_before


@pytest.mark.parametrize(
    ("arguments", "stdin_text", "expected_code", "expected_error"),
    [
        ([], "", 2, "Invalid operator command input.\n"),
        (
            ["provision-agent", "--operator-token", "plb1.argv-secret"],
            "",
            2,
            "Invalid operator command input.\n",
        ),
        (
            ["revoke-agent-credential", "--operator-token", "plb1.argv-secret"],
            "",
            2,
            "Invalid operator command input.\n",
        ),
        (
            [
                "recover",
                "--library-name",
                _LIBRARY_NAME,
                "--credential-ttl-seconds",
                "0",
            ],
            "",
            2,
            "Invalid operator command input.\n",
        ),
        (_provision_arguments(), "x" * 257, 2, "Invalid operator command input.\n"),
    ],
)
def test_invalid_inputs_fail_closed_without_echoing_input(
    cli_database: tuple[Path, Engine, list[int]],
    arguments: list[str],
    stdin_text: str,
    expected_code: int,
    expected_error: str,
) -> None:
    del cli_database

    exit_code, stdout, stderr = _run(arguments, stdin_text=stdin_text)

    assert exit_code == expected_code
    assert stdout == ""
    assert stderr == expected_error
    assert "argv-secret" not in stderr
    assert "x" * 32 not in stderr


def test_help_uses_injected_stdout_without_opening_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_settings() -> None:
        raise AssertionError("help must not load runtime configuration")

    monkeypatch.setattr(operator_cli, "Settings", unexpected_settings)

    exit_code, stdout, stderr = _run(["provision-agent", "--help"])

    assert exit_code == 0
    assert "provision-agent" in stdout
    assert "--grant" in stdout
    assert stderr == ""
