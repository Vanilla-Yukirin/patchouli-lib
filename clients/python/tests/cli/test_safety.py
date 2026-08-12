from __future__ import annotations

import importlib
import io
import json
import os
import stat
import uuid
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cli.conftest import MissingSecretStore, capabilities_body, invoke_cli, protected_headers
from patchouli_cli.config import default_config_path, default_state_path, resolve_profile
from patchouli_cli.credentials import KeyringSecretStore, resolve_token
from patchouli_cli.errors import CliError, ExitCode
from patchouli_cli.files import decode_text, read_file, read_stdin, resolve_input_root
from patchouli_cli.journal import OperationJournal, operation_fingerprint
from patchouli_cli.render import to_jsonable


class FixedSecretStore:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get_token(self, profile: str) -> str | None:
        assert profile == "default"
        return self.value


def test_profile_config_is_non_secret_strict_and_environment_overrides(
    trusted_tmp_path: Path,
) -> None:
    config = trusted_tmp_path / "config.toml"
    config.write_text(
        'version = 1\n[profiles.default]\nendpoint = "https://config.example.invalid"\n'
        'api_version = "v1"\n',
        encoding="utf-8",
    )
    profile = resolve_profile(
        profile_name=None,
        config_path=str(config),
        environ={"PATCHOULI_ENDPOINT": "https://override.example.invalid/"},
    )
    assert profile.endpoint == "https://override.example.invalid"

    config.write_text(
        'version = 1\n[profiles.default]\nendpoint = "https://config.example.invalid"\n'
        'token = "must-not-be-accepted"\n',
        encoding="utf-8",
    )
    with pytest.raises(CliError, match="unsupported or secret") as raised:
        resolve_profile(profile_name=None, config_path=str(config), environ={})
    assert raised.value.exit_code == ExitCode.CONFIG
    assert "must-not-be-accepted" not in str(raised.value)


@pytest.mark.parametrize(
    ("environment", "stdin_value", "stdin_selected", "store_value", "source"),
    [
        ({"PATCHOULI_TOKEN": "cred_environment"}, "", False, "cred_store", "environment"),
        ({"PATCHOULI_TOKEN": "cred_environment"}, "cred_stdin\n", True, None, "stdin"),
        ({}, "", False, "cred_store", "keyring"),
    ],
)
def test_credential_resolution_priority_and_repr_are_redacted(
    environment: dict[str, str],
    stdin_value: str,
    stdin_selected: bool,
    store_value: str | None,
    source: str,
) -> None:
    resolved = resolve_token(
        profile="default",
        token_stdin=stdin_selected,
        environ=environment,
        stdin=io.StringIO(stdin_value),
        secret_store=FixedSecretStore(store_value),
    )
    assert resolved.source == source
    assert "cred_" not in repr(resolved)
    assert str(resolved.token) == "<redacted>"


def test_missing_or_invalid_credential_is_safe() -> None:
    with pytest.raises(CliError, match="no caller credential"):
        resolve_token(
            profile="default",
            token_stdin=False,
            environ={},
            stdin=io.StringIO(),
            secret_store=MissingSecretStore(),
        )
    with pytest.raises(CliError, match="invalid format") as raised:
        resolve_token(
            profile="default",
            token_stdin=False,
            environ={"PATCHOULI_TOKEN": "bad token"},
            stdin=io.StringIO(),
            secret_store=MissingSecretStore(),
        )
    assert "bad token" not in str(raised.value)
    with pytest.raises(CliError, match="safe limit"):
        resolve_token(
            profile="default",
            token_stdin=True,
            environ={},
            stdin=io.StringIO("x" * 8_193),
            secret_store=MissingSecretStore(),
        )
    with pytest.raises(CliError, match="exactly one line"):
        resolve_token(
            profile="default",
            token_stdin=True,
            environ={},
            stdin=io.StringIO("first\nsecond\n"),
            secret_store=MissingSecretStore(),
        )
    with pytest.raises(CliError, match="exactly one line"):
        resolve_token(
            profile="default",
            token_stdin=True,
            environ={},
            stdin=io.StringIO("first\n\n"),
            secret_store=MissingSecretStore(),
        )
    with pytest.raises(CliError, match="safe limit"):
        resolve_token(
            profile="default",
            token_stdin=False,
            environ={"PATCHOULI_TOKEN": "x" * 8_193},
            stdin=io.StringIO(),
            secret_store=MissingSecretStore(),
        )


def test_optional_keyring_adapter_handles_missing_module_and_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        assert name == "keyring"
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", missing)
    assert KeyringSecretStore().get_token("default") is None

    def broken(name: str) -> object:
        assert name == "keyring"
        return SimpleNamespace(
            get_password=lambda service, profile: (_ for _ in ()).throw(RuntimeError())
        )

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(CliError, match="could not be read"):
        KeyringSecretStore().get_token("default")

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: SimpleNamespace(get_password=lambda service, profile: "cred_keyring"),
    )
    assert KeyringSecretStore().get_token("default") == "cred_keyring"

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: SimpleNamespace(get_password=lambda service, profile: 42),
    )
    with pytest.raises(CliError, match="invalid value"):
        KeyringSecretStore().get_token("default")


def test_file_reader_rejects_escape_symlink_and_oversize(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    safe = root / "safe.txt"
    safe.write_text("safe", encoding="utf-8")
    assert read_file("safe.txt", root=root, max_bytes=4) == b"safe"

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(CliError, match="inside"):
        read_file(str(outside), root=root, max_bytes=100)
    with pytest.raises(CliError, match="size"):
        read_file("safe.txt", root=root, max_bytes=3)

    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(CliError, match="symlink or reparse"):
        read_file("link.txt", root=root, max_bytes=100)


def test_input_root_and_journal_reject_symlinked_ancestors(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(CliError, match="must not traverse"):
        resolve_input_root(str(link / "inputs"), {})
    with pytest.raises(CliError, match="could not be secured"):
        OperationJournal(link / "state", "default")
    assert not (actual / "state").exists()


def test_input_text_and_stdin_bounds_are_explicit() -> None:
    assert decode_text(b"query\r\n", label="query", trim_terminal_newline=True) == "query"
    assert decode_text(b"query\nbody", label="query") == "query\nbody"
    assert read_stdin(io.StringIO("abc"), max_bytes=3) == b"abc"
    assert read_stdin(io.BytesIO(b"line one\r\n"), max_bytes=10) == b"line one\r\n"
    with pytest.raises(CliError, match="NUL"):
        decode_text(b"bad\x00value", label="query")
    with pytest.raises(CliError, match="safe size"):
        read_stdin(io.StringIO("abcd"), max_bytes=3)
    with pytest.raises(CliError, match="UTF-8"):
        read_stdin(io.StringIO("\ud800"), max_bytes=10)
    with pytest.raises(CliError, match="valid UTF-8"):
        decode_text(b"\xff", label="query")
    with pytest.raises(CliError, match="empty"):
        decode_text(b"\n", label="query", trim_terminal_newline=True)


def test_binary_token_stdin_is_ascii_only_and_single_line() -> None:
    resolved = resolve_token(
        profile="default",
        token_stdin=True,
        environ={},
        stdin=io.BytesIO(b"cred_binary\r\n"),
        secret_store=MissingSecretStore(),
    )
    assert resolved.source == "stdin"
    with pytest.raises(CliError, match="visible ASCII"):
        resolve_token(
            profile="default",
            token_stdin=True,
            environ={},
            stdin=io.BytesIO(b"cred_\xff\n"),
            secret_store=MissingSecretStore(),
        )


def test_journal_permissions_repr_and_mismatch_are_safe(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "state", "default")
    fingerprint = operation_fingerprint({"route": "synthetic", "content_sha256": "a" * 64})
    record = journal.prepare(
        caller_id="caller_synthetic",
        kind="archive.create",
        fingerprint=fingerprint,
        operation_id=None,
    )
    path = tmp_path / "state" / "default" / f"{record.operation_id}.json"
    assert "op_" not in repr(record)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    loaded = journal.prepare(
        caller_id="caller_synthetic",
        kind="archive.create",
        fingerprint=fingerprint,
        operation_id=record.operation_id,
    )
    assert loaded.operation_id == record.operation_id
    completed = journal.complete(record, request_id="req_synthetic")
    assert completed.status == "succeeded"
    with pytest.raises(CliError, match="does not match"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=operation_fingerprint({"changed": True}),
            operation_id=record.operation_id,
        )


def test_default_paths_and_input_root_environment_are_portable(tmp_path: Path) -> None:
    environment = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATCHOULI_INPUT_ROOT": str(tmp_path),
    }
    assert default_config_path(environment).name == "config.toml"
    assert default_state_path(environment).name == "operations"
    assert resolve_input_root(None, environment) == tmp_path
    if os.name == "nt":
        assert default_config_path({"APPDATA": str(tmp_path / "roaming")}).parts[-2:] == (
            "PatchouliLib",
            "config.toml",
        )
        assert default_state_path({"LOCALAPPDATA": str(tmp_path / "local")}).parts[-2:] == (
            "PatchouliLib",
            "operations",
        )


def test_human_output_keeps_data_on_stdout_and_diagnostics_empty(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers=protected_headers(), json=capabilities_body())

    result = invoke_cli(["capabilities"], handler=handler, tmp_path=tmp_path)
    assert result.status == ExitCode.SUCCESS
    assert result.stdout.startswith("operation: capabilities\n")
    assert '"api_versions"' in result.stdout
    assert result.stderr == ""


def test_installed_distribution_exposes_console_script() -> None:
    distribution = importlib_metadata.distribution("patchouli-client")
    assert any(
        item.name == "patchouli" and item.value == "patchouli_cli.main:entrypoint"
        for item in distribution.entry_points
        if item.group == "console_scripts"
    )


def test_unsupported_renderer_value_fails_closed() -> None:
    assert to_jsonable(ExitCode.SUCCESS) == 0
    with pytest.raises(TypeError, match="unsupported"):
        to_jsonable(object())


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not = [valid", "valid TOML"),
        ('version = 2\n[profiles.default]\nendpoint = "https://a.invalid"', "version"),
        ('unknown = true\n[profiles.default]\nendpoint = "https://a.invalid"', "top-level"),
        ("profiles = [1]\n", "profiles"),
        ('[profiles]\ndefault = "invalid"\n', "each profile"),
    ],
)
def test_profile_rejects_malformed_or_unsupported_shapes(
    contents: str, message: str, trusted_tmp_path: Path
) -> None:
    path = trusted_tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(CliError, match=message):
        resolve_profile(profile_name=None, config_path=str(path), environ={})


def test_profile_rejects_secret_setting_in_an_unselected_profile(
    trusted_tmp_path: Path,
) -> None:
    path = trusted_tmp_path / "config.toml"
    path.write_text(
        '[profiles.default]\nendpoint = "https://a.invalid"\n'
        '[profiles.other]\ntoken = "must-not-be-accepted"\n',
        encoding="utf-8",
    )
    with pytest.raises(CliError, match="unsupported or secret") as raised:
        resolve_profile(profile_name="default", config_path=str(path), environ={})
    assert "must-not-be-accepted" not in str(raised.value)


@pytest.mark.parametrize(
    ("profile", "environment", "message"),
    [
        ("bad/profile", {"PATCHOULI_ENDPOINT": "https://a.invalid"}, "profile name"),
        (None, {}, "endpoint is required"),
        (None, {"PATCHOULI_ENDPOINT": "http://a.invalid"}, "HTTPS origin"),
        (None, {"PATCHOULI_ENDPOINT": "https://user:pass@a.invalid"}, "HTTPS origin"),
        (None, {"PATCHOULI_ENDPOINT": "https://a.invalid/path"}, "HTTPS origin"),
        (None, {"PATCHOULI_ENDPOINT": "https://a.invalid:bad"}, "HTTPS origin"),
        (
            None,
            {"PATCHOULI_ENDPOINT": "https://a.invalid", "PATCHOULI_API_VERSION": "v2"},
            "only the accepted v1",
        ),
        (None, {"PATCHOULI_ENDPOINT": "https://[invalid"}, "HTTPS origin"),
    ],
)
def test_profile_rejects_invalid_names_versions_and_origins(
    profile: str | None, environment: dict[str, str], message: str
) -> None:
    with pytest.raises(CliError, match=message):
        resolve_profile(profile_name=profile, config_path=None, environ=environment)


def test_profile_rejects_missing_or_nonregular_config(trusted_tmp_path: Path) -> None:
    with pytest.raises(CliError, match="does not exist"):
        resolve_profile(
            profile_name=None,
            config_path=str(trusted_tmp_path / "missing.toml"),
            environ={"PATCHOULI_ENDPOINT": "https://a.invalid"},
        )
    directory = trusted_tmp_path / "directory.toml"
    directory.mkdir()
    with pytest.raises(CliError, match="regular file"):
        resolve_profile(
            profile_name=None,
            config_path=str(directory),
            environ={"PATCHOULI_ENDPOINT": "https://a.invalid"},
        )


def test_file_reader_rejects_missing_root_directory_and_invalid_utf8(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="could not be inspected"):
        resolve_input_root(str(tmp_path / "missing"), {})
    root_file = tmp_path / "root-file"
    root_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CliError, match="real directory"):
        resolve_input_root(str(root_file), {})

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(CliError, match="regular file"):
        read_file(str(root), root=root, max_bytes=100)
    with pytest.raises(CliError, match="inspected"):
        read_file("missing.txt", root=root, max_bytes=100)
    invalid = root / "invalid.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(CliError, match="valid UTF-8"):
        decode_text(read_file("invalid.txt", root=root, max_bytes=100), label="query")


def test_journal_rejects_invalid_ids_missing_records_and_directory_roots(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "state", "default")
    with pytest.raises(CliError, match="version-4 UUID"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint="a" * 64,
            operation_id="not-a-uuid",
        )
    missing_id = str(uuid.uuid4())
    with pytest.raises(CliError, match="could not be inspected"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint="a" * 64,
            operation_id=missing_id,
        )

    invalid_root = tmp_path / "root-file"
    invalid_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(CliError, match="directory"):
        OperationJournal(invalid_root, "default")


def test_journal_rejects_corrupt_or_unsafe_record_fields(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "state", "default")
    fingerprint = operation_fingerprint({"synthetic": True})
    record = journal.prepare(
        caller_id="caller_synthetic",
        kind="archive.create",
        fingerprint=fingerprint,
        operation_id=None,
    )
    path = tmp_path / "state" / "default" / f"{record.operation_id}.json"
    original = json.loads(path.read_text(encoding="utf-8"))

    corrupt_values: list[tuple[object, str]] = [
        ({"version": 1}, "unsupported schema"),
        ({**original, "kind": 3}, "invalid fields"),
        ({**original, "operation_id": str(uuid.uuid4())}, "identity"),
        ({**original, "fingerprint": "bad"}, "fingerprint"),
        ({**original, "status": "unknown"}, "status"),
        ({**original, "completed_at": 1}, "completion timestamp"),
        ({**original, "request_id": 1}, "request ID"),
        ({**original, "idempotency_key": "bad key"}, "idempotency key"),
    ]
    for value, message in corrupt_values:
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(CliError, match=message):
            journal.prepare(
                caller_id="caller_synthetic",
                kind="archive.create",
                fingerprint=fingerprint,
                operation_id=record.operation_id,
            )

    path.write_bytes(b"not-json")
    with pytest.raises(CliError, match="record is invalid"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=fingerprint,
            operation_id=record.operation_id,
        )
    path.write_bytes(b"x" * 16_385)
    with pytest.raises(CliError, match="size limit"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=fingerprint,
            operation_id=record.operation_id,
        )


def test_journal_rejects_nonregular_record_path(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "state", "default")
    operation_id = str(uuid.uuid4())
    path = tmp_path / "state" / "default" / f"{operation_id}.json"
    path.mkdir()
    with pytest.raises(CliError, match="regular non-reparse"):
        journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint="a" * 64,
            operation_id=operation_id,
        )
