from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NoReturn, TextIO
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Engine

from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    CallerKind,
    LocalOperatorRecovery,
    OperatorBootstrap,
    SectionAction,
)
from patchouli_lib.auth.service import utc_microseconds
from patchouli_lib.config import Settings
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService
from patchouli_lib.operator.service import (
    LocalOperatorRecoveryService,
    OperatorBootstrapService,
    OperatorService,
    ResourceNotFoundError,
)

_TOKEN_INPUT_LIMIT = 256
_MICROSECONDS_PER_SECOND = 1_000_000
_SAFE_FAILURE_MESSAGE = "Operator command failed."
_SAFE_INPUT_MESSAGE = "Invalid operator command input."
_RECOVERY_DELIVERY_MESSAGE = (
    "Credential output failed. Run local operator recovery again before continuing."
)
_AGENT_DELIVERY_MESSAGE = "Agent credential output failed. The credential was revoked."
_UNCONFIRMED_DELIVERY_MESSAGE = (
    "Agent credential output failed. Credential status could not be confirmed."
)


class _CliInputError(ValueError):
    pass


class _IncompleteOutputError(OSError):
    pass


class _DeliveryKind(StrEnum):
    LOCAL_RECOVERY = "local-recovery"
    AGENT_COMPENSATION = "agent-compensation"


@dataclass(frozen=True, slots=True)
class _HelpRequested(Exception):
    parser: argparse.ArgumentParser


class _HelpAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> NoReturn:
        del namespace, values, option_string
        raise _HelpRequested(parser)


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CliInputError


@dataclass(frozen=True, slots=True)
class _BootstrapCommand:
    library_name: str
    section_name: str
    section_description: str
    book_name: str
    book_summary: str
    operator_name: str
    operator_description: str
    credential_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class _RecoverCommand:
    library_name: str
    credential_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class _ProvisionAgentCommand:
    library_name: str
    section_name: str
    agent_name: str
    agent_description: str
    credential_ttl_seconds: int
    grants: tuple[SectionAction, ...]


@dataclass(frozen=True, slots=True)
class _RevokeAgentCredentialCommand:
    library_name: str
    caller_id: str
    credential_id: str


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class _AgentCompensation:
    actor_token: str = field(repr=False)
    library_id: str
    caller_id: str
    credential_id: str
    occurred_at: int

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(actor_token=<redacted>, "
            f"library_id={self.library_id!r}, caller_id={self.caller_id!r}, "
            f"credential_id={self.credential_id!r}, occurred_at={self.occurred_at!r})"
        )


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class _SecretDelivery:
    value: str = field(repr=False)
    kind: _DeliveryKind
    compensation: _AgentCompensation | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(value=<redacted>, kind={self.kind!r}, "
            f"compensation={self.compensation!r})"
        )


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-h",
        "--help",
        action=_HelpAction,
        nargs=0,
        help="show this help message and exit",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(
        prog="patchouli-operator",
        description="Local-only PatchouliLib operator administration.",
        add_help=False,
    )
    _add_help(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap",
        help="seed one Library/Section/Book and bootstrap its operator",
        add_help=False,
    )
    _add_help(bootstrap)
    bootstrap.add_argument("--library-name", required=True)
    bootstrap.add_argument("--section-name", required=True)
    bootstrap.add_argument("--section-description", default="")
    bootstrap.add_argument("--book-name", required=True)
    bootstrap.add_argument("--book-summary", default="")
    bootstrap.add_argument("--operator-name", required=True)
    bootstrap.add_argument("--operator-description", default="")
    bootstrap.add_argument("--credential-ttl-seconds", required=True, type=int)

    recover = subparsers.add_parser(
        "recover",
        help="retire active operator credentials and issue one replacement",
        add_help=False,
    )
    _add_help(recover)
    recover.add_argument("--library-name", required=True)
    recover.add_argument("--credential-ttl-seconds", required=True, type=int)

    provision = subparsers.add_parser(
        "provision-agent",
        help="create one Agent, credential, and exact Section grants",
        add_help=False,
    )
    _add_help(provision)
    provision.add_argument("--library-name", required=True)
    provision.add_argument("--section-name", required=True)
    provision.add_argument("--agent-name", required=True)
    provision.add_argument("--agent-description", default="")
    provision.add_argument("--credential-ttl-seconds", required=True, type=int)
    provision.add_argument(
        "--grant",
        required=True,
        action="append",
        choices=tuple(action.value for action in SectionAction),
        help="exact Section action; repeat for additional actions",
    )

    revoke = subparsers.add_parser(
        "revoke-agent-credential",
        help="immediately revoke one Agent credential",
        add_help=False,
    )
    _add_help(revoke)
    revoke.add_argument("--library-name", required=True)
    revoke.add_argument("--caller-id", required=True)
    revoke.add_argument("--credential-id", required=True)
    return parser


def _namespace_value[T](namespace: argparse.Namespace, name: str, value_type: type[T]) -> T:
    value = getattr(namespace, name, None)
    if not isinstance(value, value_type):
        raise _CliInputError
    return value


def _parse_command(
    argv: Sequence[str],
) -> _BootstrapCommand | _RecoverCommand | _ProvisionAgentCommand | _RevokeAgentCredentialCommand:
    namespace = _build_parser().parse_args(list(argv))
    command = _namespace_value(namespace, "command", str)
    library_name = _namespace_value(namespace, "library_name", str)
    if command == "bootstrap":
        return _BootstrapCommand(
            library_name=library_name,
            section_name=_namespace_value(namespace, "section_name", str),
            section_description=_namespace_value(namespace, "section_description", str),
            book_name=_namespace_value(namespace, "book_name", str),
            book_summary=_namespace_value(namespace, "book_summary", str),
            operator_name=_namespace_value(namespace, "operator_name", str),
            operator_description=_namespace_value(namespace, "operator_description", str),
            credential_ttl_seconds=_namespace_value(
                namespace,
                "credential_ttl_seconds",
                int,
            ),
        )
    if command == "recover":
        return _RecoverCommand(
            library_name=library_name,
            credential_ttl_seconds=_namespace_value(
                namespace,
                "credential_ttl_seconds",
                int,
            ),
        )
    if command == "provision-agent":
        raw_grants = _namespace_value(namespace, "grant", list)
        if not all(isinstance(value, str) for value in raw_grants):
            raise _CliInputError
        grants = tuple(dict.fromkeys(SectionAction(value) for value in raw_grants))
        return _ProvisionAgentCommand(
            library_name=library_name,
            section_name=_namespace_value(namespace, "section_name", str),
            agent_name=_namespace_value(namespace, "agent_name", str),
            agent_description=_namespace_value(namespace, "agent_description", str),
            credential_ttl_seconds=_namespace_value(
                namespace,
                "credential_ttl_seconds",
                int,
            ),
            grants=grants,
        )
    if command == "revoke-agent-credential":
        return _RevokeAgentCredentialCommand(
            library_name=library_name,
            caller_id=_namespace_value(namespace, "caller_id", str),
            credential_id=_namespace_value(namespace, "credential_id", str),
        )
    raise _CliInputError


def _expires_at(now: int, ttl_seconds: int) -> int:
    if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise _CliInputError
    expires_at = now + ttl_seconds * _MICROSECONDS_PER_SECOND
    if expires_at > MAX_RFC3339_TIMESTAMP_MICROSECONDS:
        raise _CliInputError
    return expires_at


def _request_id() -> str:
    return f"req_local_{uuid4().hex}"


def _read_operator_token(stdin: TextIO) -> str:
    presented = stdin.read(_TOKEN_INPUT_LIMIT + 1)
    if len(presented) > _TOKEN_INPUT_LIMIT:
        raise _CliInputError
    if presented.endswith("\r\n"):
        token = presented[:-2]
    elif presented.endswith("\n"):
        token = presented[:-1]
    else:
        token = presented
    if not token or any(character.isspace() for character in token):
        raise _CliInputError
    return token


def _require_library(repository: LibraryRepository, name: str) -> str:
    library = repository.find_library_by_name(name)
    if library is None:
        raise ResourceNotFoundError
    return library.id


def _bootstrap(engine: Engine, command: _BootstrapCommand) -> _SecretDelivery:
    now = utc_microseconds()
    expires_at = _expires_at(now, command.credential_ttl_seconds)
    with immediate_transaction(engine) as connection:
        structure = LibrarySeedService(
            LibraryRepository(connection),
            clock=lambda: now,
        ).seed(
            LibraryStructureSeed(
                library_name=command.library_name,
                section_name=command.section_name,
                section_description=command.section_description,
                book_name=command.book_name,
                book_summary=command.book_summary,
            )
        )
        result = OperatorBootstrapService(
            AuthRepository(connection),
            clock=lambda: now,
        ).bootstrap(
            OperatorBootstrap(
                library_id=structure.library.id,
                operator_name=command.operator_name,
                operator_description=command.operator_description,
                credential_expires_at=expires_at,
                request_id=_request_id(),
            )
        )
    return _SecretDelivery(
        value=result.credential.value,
        kind=_DeliveryKind.LOCAL_RECOVERY,
    )


def _recover(engine: Engine, command: _RecoverCommand) -> _SecretDelivery:
    now = utc_microseconds()
    expires_at = _expires_at(now, command.credential_ttl_seconds)
    with immediate_transaction(engine) as connection:
        library_id = _require_library(LibraryRepository(connection), command.library_name)
        result = LocalOperatorRecoveryService(
            AuthRepository(connection),
            clock=lambda: now,
        ).recover(
            LocalOperatorRecovery(
                library_id=library_id,
                credential_expires_at=expires_at,
                request_id=_request_id(),
            )
        )
    return _SecretDelivery(
        value=result.credential.value,
        kind=_DeliveryKind.LOCAL_RECOVERY,
    )


def _provision_agent(
    engine: Engine,
    command: _ProvisionAgentCommand,
    stdin: TextIO,
) -> _SecretDelivery:
    actor_token = _read_operator_token(stdin)
    now = utc_microseconds()
    expires_at = _expires_at(now, command.credential_ttl_seconds)
    with immediate_transaction(engine) as connection:
        library_repository = LibraryRepository(connection)
        library_id = _require_library(library_repository, command.library_name)
        section = library_repository.find_section_by_name(library_id, command.section_name)
        if section is None:
            raise ResourceNotFoundError
        service = OperatorService(
            AuthRepository(connection),
            clock=lambda: now,
        )
        caller = service.create_agent_caller(
            actor_token,
            library_id=library_id,
            name=command.agent_name,
            description=command.agent_description,
            request_id=_request_id(),
        )
        issued = service.create_credential(
            actor_token,
            library_id=library_id,
            caller_id=caller.id,
            expires_at=expires_at,
            request_id=_request_id(),
        )
        for action in command.grants:
            service.add_grant(
                actor_token,
                library_id=library_id,
                caller_id=caller.id,
                section_id=section.id,
                action=action,
                request_id=_request_id(),
            )
    return _SecretDelivery(
        value=issued.value,
        kind=_DeliveryKind.AGENT_COMPENSATION,
        compensation=_AgentCompensation(
            actor_token=actor_token,
            library_id=library_id,
            caller_id=caller.id,
            credential_id=issued.credential.id,
            occurred_at=now,
        ),
    )


def _revoke_agent_credential(
    engine: Engine,
    command: _RevokeAgentCredentialCommand,
    stdin: TextIO,
) -> None:
    actor_token = _read_operator_token(stdin)
    now = utc_microseconds()
    with immediate_transaction(engine) as connection:
        library_id = _require_library(LibraryRepository(connection), command.library_name)
        repository = AuthRepository(connection)
        caller = repository.get_caller(library_id, command.caller_id)
        if caller is None or caller.kind is not CallerKind.AGENT:
            raise ResourceNotFoundError
        OperatorService(
            repository,
            clock=lambda: now,
        ).revoke_credential(
            actor_token,
            library_id=library_id,
            caller_id=caller.id,
            credential_id=command.credential_id,
            request_id=_request_id(),
        )


def _execute(
    engine: Engine,
    command: (
        _BootstrapCommand | _RecoverCommand | _ProvisionAgentCommand | _RevokeAgentCredentialCommand
    ),
    stdin: TextIO,
) -> _SecretDelivery | None:
    if isinstance(command, _BootstrapCommand):
        return _bootstrap(engine, command)
    if isinstance(command, _RecoverCommand):
        return _recover(engine, command)
    if isinstance(command, _ProvisionAgentCommand):
        return _provision_agent(engine, command, stdin)
    _revoke_agent_credential(engine, command, stdin)
    return None


def _output_position(stdout: TextIO) -> int | None:
    try:
        if stdout.seekable():
            return stdout.tell()
    except BaseException:
        return None
    return None


def _discard_buffered_output(stdout: TextIO, position: int | None) -> None:
    if position is None:
        return
    try:
        stdout.seek(position)
        stdout.truncate()
    except BaseException:
        pass


def _write_redacted(stderr: TextIO, message: str) -> None:
    try:
        stderr.write(f"{message}\n")
        stderr.flush()
    except BaseException:
        pass


def _revoke_failed_agent_delivery(engine: Engine, compensation: _AgentCompensation) -> bool:
    try:
        with immediate_transaction(engine) as connection:
            OperatorService(
                AuthRepository(connection),
                clock=lambda: compensation.occurred_at,
            ).revoke_credential(
                compensation.actor_token,
                library_id=compensation.library_id,
                caller_id=compensation.caller_id,
                credential_id=compensation.credential_id,
                request_id=_request_id(),
            )
    except BaseException:
        return False
    return True


def _deliver_secret(
    engine: Engine,
    delivery: _SecretDelivery,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    position = _output_position(stdout)
    try:
        payload = f"{delivery.value}\n"
        written = stdout.write(payload)
        if written != len(payload):
            raise _IncompleteOutputError
        stdout.flush()
    except BaseException:
        _discard_buffered_output(stdout, position)
        if delivery.kind is _DeliveryKind.LOCAL_RECOVERY:
            _write_redacted(stderr, _RECOVERY_DELIVERY_MESSAGE)
            return 1
        compensation = delivery.compensation
        if compensation is not None and _revoke_failed_agent_delivery(engine, compensation):
            _write_redacted(stderr, _AGENT_DELIVERY_MESSAGE)
            return 1
        _write_redacted(stderr, _UNCONFIRMED_DELIVERY_MESSAGE)
        return 1
    return 0


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one local-only operator command without exposing secret arguments."""

    arguments = sys.argv[1:] if argv is None else argv
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    try:
        command = _parse_command(arguments)
    except _HelpRequested as requested:
        requested.parser.print_help(output_stream)
        return 0
    except _CliInputError:
        error_stream.write(f"{_SAFE_INPUT_MESSAGE}\n")
        return 2

    engine: Engine | None = None
    try:
        settings = Settings()
        engine = build_engine(settings.database_url)
        delivery = _execute(engine, command, input_stream)
        if delivery is None:
            return 0
        return _deliver_secret(engine, delivery, output_stream, error_stream)
    except (ValidationError, _CliInputError):
        error_stream.write(f"{_SAFE_INPUT_MESSAGE}\n")
        return 2
    except Exception:
        error_stream.write(f"{_SAFE_FAILURE_MESSAGE}\n")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
