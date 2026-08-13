from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from patchouli_lib import backup_cli
from patchouli_lib.backup import (
    BackupArtifactIdentity,
    BackupCancelledError,
    BackupOperationError,
    BackupTimeoutError,
    create_backup,
    validate_database,
    verify_backup_bundle,
)

APP_VERSION = "0.1.0a0"
IDENTITY = BackupArtifactIdentity(
    identity="synthetic/source@immutable",
    digest="sha256:" + "ab" * 32,
)


class _BrokenOutput(StringIO):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self._failure = failure

    def write(self, value: str) -> int:
        if self._failure == "write":
            super().write(value[: len(value) // 2])
            raise OSError("synthetic output failure with private-looking detail")
        if self._failure == "short":
            super().write(value[: len(value) // 2])
            return len(value) // 2
        if self._failure == "none":
            super().write(value)
            return None  # type: ignore[return-value]
        return super().write(value)

    def flush(self) -> None:
        if self._failure == "flush":
            raise OSError("synthetic flush failure with private-looking detail")
        super().flush()


class _BrokenError(StringIO):
    def write(self, value: str) -> int:
        del value
        raise OSError("synthetic stderr failure")


def _run(
    argv: Sequence[str],
    *,
    stdout: StringIO | None = None,
    stderr: StringIO | None = None,
) -> tuple[int, str, str]:
    output = StringIO() if stdout is None else stdout
    error = StringIO() if stderr is None else stderr
    exit_code = backup_cli.main(argv, stdout=output, stderr=error)
    return exit_code, output.getvalue(), error.getvalue()


def _create_arguments(bundle: Path, *, output_format: str = "text") -> list[str]:
    return [
        "create",
        "--bundle",
        str(bundle),
        "--artifact-identity",
        IDENTITY.identity,
        "--artifact-digest",
        IDENTITY.digest,
        "--format",
        output_format,
    ]


def _create_bundle(engine: Engine, bundle: Path) -> None:
    create_backup(
        engine,
        bundle,
        artifact_identity=IDENTITY,
        app_version=APP_VERSION,
    )


@pytest.fixture(autouse=True)
def exact_app_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_cli, "__version__", APP_VERSION)


def test_create_reads_database_url_only_from_environment_and_emits_safe_json(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(str(complete_engine.url.database))
    source_url = f"sqlite:///{source_path.as_posix()}"
    bundle = tmp_path / "created-bundle"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", source_url)

    exit_code, stdout, stderr = _run(_create_arguments(bundle, output_format="json"))

    assert exit_code == backup_cli.ExitCode.SUCCESS
    assert stderr == ""
    metadata = json.loads(stdout)
    assert metadata == {
        "artifact_digest": IDENTITY.digest,
        "byte_size": metadata["byte_size"],
        "operation": "create",
        "schema_revision": "20260813_0006",
        "sha256": metadata["sha256"],
        "state": "created",
    }
    assert isinstance(metadata["byte_size"], int) and metadata["byte_size"] > 0
    assert len(metadata["sha256"]) == 64
    assert str(bundle) not in stdout
    assert str(source_path) not in stdout
    assert source_url not in stdout
    assert sorted(item.name for item in bundle.iterdir()) == ["database.sqlite", "manifest.json"]


def test_verify_is_read_only_and_text_output_contains_no_path(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "verified-bundle"
    _create_bundle(complete_engine, bundle)
    before = {item.name: item.read_bytes() for item in bundle.iterdir()}

    def unexpected_environment() -> str:
        raise AssertionError("verify must not load a source database URL")

    monkeypatch.setattr(backup_cli, "_database_url_from_environment", unexpected_environment)

    exit_code, stdout, stderr = _run(["verify", "--bundle", str(bundle)])

    assert exit_code == backup_cli.ExitCode.SUCCESS
    assert stderr == ""
    assert "operation=verify\n" in stdout
    assert "state=verified\n" in stdout
    assert IDENTITY.identity not in stdout
    assert str(bundle) not in stdout
    assert {item.name: item.read_bytes() for item in bundle.iterdir()} == before


def test_restore_creates_only_a_new_inactive_destination(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "restore-bundle"
    _create_bundle(complete_engine, bundle)
    destination = tmp_path / "restored.sqlite"

    def unexpected_environment() -> str:
        raise AssertionError("restore must not load a source database URL")

    monkeypatch.setattr(backup_cli, "_database_url_from_environment", unexpected_environment)

    exit_code, stdout, stderr = _run(
        [
            "restore",
            "--bundle",
            str(bundle),
            "--destination",
            str(destination),
            "--format",
            "json",
        ]
    )

    assert exit_code == backup_cli.ExitCode.SUCCESS
    assert stderr == ""
    assert destination.is_file()
    metadata = json.loads(stdout)
    assert metadata["operation"] == "restore"
    assert metadata["state"] == "inactive"
    assert metadata["activation_authorized"] is False
    assert str(bundle) not in stdout
    assert str(destination) not in stdout

    original = destination.read_bytes()
    repeated_code, repeated_stdout, repeated_stderr = _run(
        ["restore", "--bundle", str(bundle), "--destination", str(destination)]
    )
    assert repeated_code == backup_cli.ExitCode.INVALID_CONFIGURATION
    assert repeated_stdout == ""
    assert repeated_stderr == "Backup configuration is invalid.\n"
    assert destination.read_bytes() == original


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["create", "--database-url", "sqlite:///argv-secret.sqlite"],
        ["verify", "--bundle", "relative-bundle", "extra-private-value"],
        ["restore", "--bundle", "relative", "--destination"],
        [
            "create",
            "--bundle",
            "relative",
            "--artifact-identity",
            "identity with spaces",
            "--artifact-digest",
            "not-a-digest",
        ],
    ],
)
def test_malformed_and_extra_arguments_are_redacted(arguments: list[str]) -> None:
    exit_code, stdout, stderr = _run(arguments)

    assert exit_code == backup_cli.ExitCode.INVALID_INPUT
    assert stdout == ""
    assert stderr == "Invalid backup command input.\n"
    assert "secret" not in stderr
    assert "private" not in stderr


def test_uri_shaped_artifact_identity_stays_only_in_manifest(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_shaped_identity = "oci://registry.example.invalid/private/repository@sha256"
    source_path = Path(str(complete_engine.url.database))
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{source_path.as_posix()}")
    bundle = tmp_path / "identity-bundle"
    arguments = _create_arguments(bundle, output_format="json")
    arguments[arguments.index(IDENTITY.identity)] = private_shaped_identity

    exit_code, stdout, stderr = _run(arguments)

    assert exit_code == backup_cli.ExitCode.SUCCESS
    assert private_shaped_identity not in stdout
    assert private_shaped_identity not in stderr
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_identity"] == private_shaped_identity


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "sqlite:///:memory:",
        "sqlite:///relative.sqlite",
        "sqlite:////missing-parent/source.sqlite?uri=true",
        "postgresql:///synthetic",
    ],
)
def test_create_requires_existing_absolute_sqlite_url_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str | None,
) -> None:
    if database_url is None:
        monkeypatch.delenv("PATCHOULI_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    bundle = tmp_path / "not-created"

    exit_code, stdout, stderr = _run(_create_arguments(bundle))

    assert exit_code == backup_cli.ExitCode.INVALID_CONFIGURATION
    assert stdout == ""
    assert stderr == "Backup configuration is invalid.\n"
    assert not bundle.exists()
    assert not database_url or database_url not in stderr


def test_artifact_validation_failure_is_fixed_and_does_not_echo_paths(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "corrupt-bundle"
    _create_bundle(complete_engine, bundle)
    (bundle / "manifest.json").write_bytes(b"private-looking malformed manifest")

    exit_code, stdout, stderr = _run(["verify", "--bundle", str(bundle)])

    assert exit_code == backup_cli.ExitCode.VALIDATION_FAILED
    assert stdout == ""
    assert stderr == "Backup artifact validation failed.\n"
    assert str(bundle) not in stderr
    assert "private-looking" not in stderr


def test_unknown_application_version_fails_before_create_verify_or_restore(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(str(complete_engine.url.database))
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{source_path.as_posix()}")
    bundle = tmp_path / "known-version-bundle"
    _create_bundle(complete_engine, bundle)
    before = {item.name: item.read_bytes() for item in bundle.iterdir()}
    unknown_bundle = tmp_path / "unknown-version-bundle"
    destination = tmp_path / "unknown-version-restored.sqlite"
    monkeypatch.setattr(backup_cli, "__version__", "0.0.0+unknown")

    for arguments in (
        _create_arguments(unknown_bundle),
        ["verify", "--bundle", str(bundle)],
        ["restore", "--bundle", str(bundle), "--destination", str(destination)],
    ):
        exit_code, stdout, stderr = _run(arguments)
        assert exit_code == backup_cli.ExitCode.INVALID_CONFIGURATION
        assert stdout == ""
        assert stderr == "Backup configuration is invalid.\n"

    assert not unknown_bundle.exists()
    assert not destination.exists()
    assert {item.name: item.read_bytes() for item in bundle.iterdir()} == before


@pytest.mark.parametrize("command", ["verify", "restore"])
def test_mismatched_known_application_version_fails_exact_compatibility(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    bundle = tmp_path / f"version-mismatch-{command}"
    _create_bundle(complete_engine, bundle)
    destination = tmp_path / "not-restored.sqlite"
    monkeypatch.setattr(backup_cli, "__version__", "0.1.0a1")
    arguments = [command, "--bundle", str(bundle)]
    if command == "restore":
        arguments.extend(("--destination", str(destination)))

    exit_code, stdout, stderr = _run(arguments)

    assert exit_code == backup_cli.ExitCode.VALIDATION_FAILED
    assert stdout == ""
    assert stderr == "Backup artifact validation failed.\n"
    assert not destination.exists()


def test_create_output_failure_preserves_published_verifiable_bundle(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path(str(complete_engine.url.database))
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", f"sqlite:///{source_path.as_posix()}")
    bundle = tmp_path / "created-before-output-failure"

    exit_code, stdout, stderr = _run(
        _create_arguments(bundle),
        stdout=_BrokenOutput("flush"),
    )

    assert exit_code == backup_cli.ExitCode.OPERATION_FAILED
    assert stdout == ""
    assert stderr == "Backup command output failed.\n"
    manifest = verify_backup_bundle(bundle, app_version=APP_VERSION)
    assert manifest.artifact_identity == IDENTITY.identity


def test_restore_output_failure_preserves_inactive_verifiable_destination(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "restore-output-bundle"
    _create_bundle(complete_engine, bundle)
    destination = tmp_path / "restored-before-output-failure.sqlite"

    exit_code, stdout, stderr = _run(
        ["restore", "--bundle", str(bundle), "--destination", str(destination)],
        stdout=_BrokenOutput("flush"),
    )

    assert exit_code == backup_cli.ExitCode.OPERATION_FAILED
    assert stdout == ""
    assert stderr == "Backup command output failed.\n"
    assert destination.is_file()
    report = validate_database(destination)
    assert report.schema_revision == "20260813_0006"
    assert destination.read_bytes() == (bundle / "database.sqlite").read_bytes()


@pytest.mark.parametrize("failure", ["write", "short", "none", "flush"])
def test_output_delivery_failure_never_returns_success_or_leaks_raw_error(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    metadata: Mapping[str, str | int | bool] = {
        "operation": "verify",
        "state": "verified",
    }
    monkeypatch.setattr(backup_cli, "_execute", lambda _command: metadata)
    broken = _BrokenOutput(failure)

    exit_code, stdout, stderr = _run(
        ["verify", "--bundle", "C:/synthetic/bundle"],
        stdout=broken,
    )

    assert exit_code == backup_cli.ExitCode.OPERATION_FAILED
    assert stdout == ""
    assert stderr == "Backup command output failed.\n"
    assert "private-looking" not in stderr


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_error"),
    [
        (
            BackupCancelledError(),
            backup_cli.ExitCode.CANCELLED,
            "Backup command was cancelled.\n",
        ),
        (
            BackupTimeoutError(),
            backup_cli.ExitCode.TIMED_OUT,
            "Backup command timed out.\n",
        ),
        (
            BackupOperationError(),
            backup_cli.ExitCode.OPERATION_FAILED,
            "Backup command failed.\n",
        ),
        (
            RuntimeError("raw SQLite detail at C:/private/database.sqlite"),
            backup_cli.ExitCode.OPERATION_FAILED,
            "Backup command failed.\n",
        ),
        (
            KeyboardInterrupt(),
            backup_cli.ExitCode.CANCELLED,
            "Backup command was cancelled.\n",
        ),
    ],
)
def test_operation_failures_and_cancellation_use_stable_redacted_codes(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_code: backup_cli.ExitCode,
    expected_error: str,
) -> None:
    def fail(_command: object) -> Any:
        raise failure

    monkeypatch.setattr(backup_cli, "_execute", fail)

    exit_code, stdout, stderr = _run(["verify", "--bundle", "C:/synthetic/bundle"])

    assert exit_code == expected_code
    assert stdout == ""
    assert stderr == expected_error
    assert "private" not in stderr
    assert "SQLite" not in stderr


def test_help_does_not_read_runtime_configuration_and_handles_output_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_environment() -> str:
        raise AssertionError("help must not load runtime configuration")

    monkeypatch.setattr(backup_cli, "_database_url_from_environment", unexpected_environment)

    exit_code, stdout, stderr = _run(["restore", "--help"])

    assert exit_code == backup_cli.ExitCode.SUCCESS
    assert "restore" in stdout
    assert "--destination" in stdout
    assert stderr == ""

    broken = _BrokenOutput("short")
    broken_code, broken_stdout, broken_stderr = _run(["--help"], stdout=broken)
    assert broken_code == backup_cli.ExitCode.OPERATION_FAILED
    assert broken_stdout == ""
    assert broken_stderr == "Backup command output failed.\n"


def test_stderr_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_cli,
        "_execute",
        lambda _command: (_ for _ in ()).throw(BackupOperationError()),
    )

    exit_code, stdout, _stderr = _run(
        ["verify", "--bundle", "C:/synthetic/bundle"],
        stderr=_BrokenError(),
    )

    assert exit_code == backup_cli.ExitCode.OPERATION_FAILED
    assert stdout == ""
