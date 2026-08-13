"""Callable module boundary for the experimental SQLite backup prototype.

Invoke this prototype as ``python -m patchouli_lib.backup_cli``. Console-script
registration is an integration concern outside this module. These commands
create or inspect artifacts only. Restore always materializes an inactive
database file and never performs cutover, migration, credential changes, or
deployment work.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import NoReturn, TextIO

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from patchouli_lib import __version__
from patchouli_lib.backup import (
    BackupArtifactIdentity,
    BackupCancelledError,
    BackupConfigurationError,
    BackupDatabaseError,
    BackupManifestError,
    BackupManifestV1,
    BackupOperationError,
    BackupTimeoutError,
    create_backup,
    restore_backup,
    verify_backup_bundle,
)
from patchouli_lib.database import build_engine

_SAFE_INPUT_MESSAGE = "Invalid backup command input."
_SAFE_CONFIGURATION_MESSAGE = "Backup configuration is invalid."
_SAFE_VALIDATION_MESSAGE = "Backup artifact validation failed."
_SAFE_TIMEOUT_MESSAGE = "Backup command timed out."
_SAFE_CANCELLED_MESSAGE = "Backup command was cancelled."
_SAFE_OPERATION_MESSAGE = "Backup command failed."
_SAFE_OUTPUT_MESSAGE = "Backup command output failed."


class ExitCode(IntEnum):
    """Stable process exit codes for the experimental local command boundary."""

    SUCCESS = 0
    OPERATION_FAILED = 1
    INVALID_INPUT = 2
    INVALID_CONFIGURATION = 3
    VALIDATION_FAILED = 4
    TIMED_OUT = 5
    CANCELLED = 130


class _CliInputError(ValueError):
    pass


class _IncompleteOutputError(OSError):
    pass


class _OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


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
class _CreateCommand:
    bundle: Path
    artifact_identity: BackupArtifactIdentity
    output_format: _OutputFormat


@dataclass(frozen=True, slots=True)
class _VerifyCommand:
    bundle: Path
    output_format: _OutputFormat


@dataclass(frozen=True, slots=True)
class _RestoreCommand:
    bundle: Path
    destination: Path
    output_format: _OutputFormat


_Command = _CreateCommand | _VerifyCommand | _RestoreCommand
_MetadataValue = str | int | bool


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-h",
        "--help",
        action=_HelpAction,
        nargs=0,
        help="show this help message and exit",
    )


def _add_output_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=tuple(item.value for item in _OutputFormat),
        default=_OutputFormat.TEXT.value,
        help="emit bounded text or canonical JSON metadata",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(
        prog="patchouli-backup",
        description="Experimental local SQLite backup, verification, and inactive restore.",
        add_help=False,
    )
    _add_help(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        help="create a new immutable backup bundle",
        add_help=False,
    )
    _add_help(create)
    create.add_argument("--bundle", required=True)
    create.add_argument("--artifact-identity", required=True)
    create.add_argument("--artifact-digest", required=True)
    _add_output_format(create)

    verify = subparsers.add_parser(
        "verify",
        help="verify a bundle without modifying it",
        add_help=False,
    )
    _add_help(verify)
    verify.add_argument("--bundle", required=True)
    _add_output_format(verify)

    restore = subparsers.add_parser(
        "restore",
        help="restore into one new inactive database destination",
        add_help=False,
    )
    _add_help(restore)
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--destination", required=True)
    _add_output_format(restore)
    return parser


def _parse_command(arguments: Sequence[str]) -> _Command:
    namespace = _build_parser().parse_args(list(arguments))
    try:
        output_format = _OutputFormat(namespace.format)
        if namespace.command == "create":
            identity = BackupArtifactIdentity(
                identity=namespace.artifact_identity,
                digest=namespace.artifact_digest,
            )
            return _CreateCommand(Path(namespace.bundle), identity, output_format)
        if namespace.command == "verify":
            return _VerifyCommand(Path(namespace.bundle), output_format)
        if namespace.command == "restore":
            return _RestoreCommand(
                Path(namespace.bundle),
                Path(namespace.destination),
                output_format,
            )
    except (BackupManifestError, OSError, TypeError, ValueError):
        raise _CliInputError from None
    raise _CliInputError


def _database_url_from_environment() -> str:
    database_url = os.environ.get("PATCHOULI_DATABASE_URL")
    if database_url is None or not database_url:
        raise BackupConfigurationError
    try:
        parsed = make_url(database_url)
        database = parsed.database
        if (
            parsed.get_backend_name() != "sqlite"
            or parsed.query
            or database is None
            or database in {"", ":memory:"}
        ):
            raise BackupConfigurationError
        source = Path(database)
        if not source.is_absolute() or not source.is_file():
            raise BackupConfigurationError
    except BackupConfigurationError:
        raise
    except (OSError, TypeError, ValueError):
        raise BackupConfigurationError from None
    return database_url


def _application_version() -> str:
    if not isinstance(__version__, str) or not __version__ or __version__.endswith("+unknown"):
        raise BackupConfigurationError
    return __version__


def _manifest_metadata(
    operation: str,
    state: str,
    manifest: BackupManifestV1,
    *,
    activation_authorized: bool | None = None,
) -> dict[str, _MetadataValue]:
    metadata: dict[str, _MetadataValue] = {
        "operation": operation,
        "state": state,
        "artifact_digest": manifest.artifact_digest,
        "schema_revision": manifest.schema_revision,
        "byte_size": manifest.byte_size,
        "sha256": manifest.sha256,
    }
    if activation_authorized is not None:
        metadata["activation_authorized"] = activation_authorized
    return metadata


def _execute(command: _Command) -> Mapping[str, _MetadataValue]:
    app_version = _application_version()
    if isinstance(command, _CreateCommand):
        engine: Engine | None = None
        try:
            engine = build_engine(_database_url_from_environment())
            backup_result = create_backup(
                engine,
                command.bundle,
                artifact_identity=command.artifact_identity,
                app_version=app_version,
            )
        finally:
            if engine is not None:
                engine.dispose()
        return _manifest_metadata("create", "created", backup_result.manifest)
    if isinstance(command, _VerifyCommand):
        manifest = verify_backup_bundle(command.bundle, app_version=app_version)
        return _manifest_metadata("verify", "verified", manifest)
    restore_result = restore_backup(
        command.bundle,
        command.destination,
        app_version=app_version,
    )
    return _manifest_metadata(
        "restore",
        "inactive",
        restore_result.manifest,
        activation_authorized=False,
    )


def _render(metadata: Mapping[str, _MetadataValue], output_format: _OutputFormat) -> str:
    if output_format is _OutputFormat.JSON:
        return (
            json.dumps(
                metadata,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    return "".join(
        f"{key}={str(value).lower() if isinstance(value, bool) else value}\n"
        for key, value in metadata.items()
    )


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
        payload = f"{message}\n"
        written = stderr.write(payload)
        if written != len(payload):
            return
        stderr.flush()
    except BaseException:
        pass


def _deliver(payload: str, stdout: TextIO, stderr: TextIO) -> ExitCode:
    position = _output_position(stdout)
    try:
        written = stdout.write(payload)
        if written != len(payload):
            raise _IncompleteOutputError
        stdout.flush()
    except BaseException:
        _discard_buffered_output(stdout, position)
        _write_redacted(stderr, _SAFE_OUTPUT_MESSAGE)
        return ExitCode.OPERATION_FAILED
    return ExitCode.SUCCESS


def _command_output_format(command: _Command) -> _OutputFormat:
    return command.output_format


def main(
    argv: Sequence[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one local experimental backup command with disclosure-safe failures."""

    arguments = sys.argv[1:] if argv is None else argv
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    try:
        command = _parse_command(arguments)
    except _HelpRequested as requested:
        return int(_deliver(requested.parser.format_help(), output_stream, error_stream))
    except _CliInputError:
        _write_redacted(error_stream, _SAFE_INPUT_MESSAGE)
        return int(ExitCode.INVALID_INPUT)

    try:
        metadata = _execute(command)
        payload = _render(metadata, _command_output_format(command))
    except BackupCancelledError:
        _write_redacted(error_stream, _SAFE_CANCELLED_MESSAGE)
        return int(ExitCode.CANCELLED)
    except BackupTimeoutError:
        _write_redacted(error_stream, _SAFE_TIMEOUT_MESSAGE)
        return int(ExitCode.TIMED_OUT)
    except BackupConfigurationError:
        _write_redacted(error_stream, _SAFE_CONFIGURATION_MESSAGE)
        return int(ExitCode.INVALID_CONFIGURATION)
    except (BackupManifestError, BackupDatabaseError):
        _write_redacted(error_stream, _SAFE_VALIDATION_MESSAGE)
        return int(ExitCode.VALIDATION_FAILED)
    except BackupOperationError:
        _write_redacted(error_stream, _SAFE_OPERATION_MESSAGE)
        return int(ExitCode.OPERATION_FAILED)
    except KeyboardInterrupt:
        _write_redacted(error_stream, _SAFE_CANCELLED_MESSAGE)
        return int(ExitCode.CANCELLED)
    except BaseException:
        _write_redacted(error_stream, _SAFE_OPERATION_MESSAGE)
        return int(ExitCode.OPERATION_FAILED)

    return int(_deliver(payload, output_stream, error_stream))


if __name__ == "__main__":
    raise SystemExit(main())
