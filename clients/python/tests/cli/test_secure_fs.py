from __future__ import annotations

import importlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from cli.conftest import invoke_cli
from patchouli_cli.config import resolve_profile
from patchouli_cli.errors import CliError, ExitCode
from patchouli_cli.files import open_input_root, read_file
from patchouli_cli.journal import OperationJournal, operation_fingerprint
from patchouli_cli.secure_fs import (
    SecureDirectory,
    SecureFile,
    current_user_only,
)


def test_config_read_stays_bound_to_verified_file_handle(
    trusted_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = trusted_tmp_path / "config.toml"
    moved = trusted_tmp_path / "opened-config.toml"
    config.write_text(
        '[profiles.default]\nendpoint = "https://verified.example.invalid"\n',
        encoding="utf-8",
    )
    original_read = SecureFile.read
    replaced = False

    def replace_after_open(file: SecureFile, max_bytes: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            config.rename(moved)
            config.write_text(
                '[profiles.default]\nendpoint = "https://replacement.example.invalid"\n',
                encoding="utf-8",
            )
        return original_read(file, max_bytes)

    monkeypatch.setattr(SecureFile, "read", replace_after_open)
    profile = resolve_profile(profile_name=None, config_path=str(config), environ={})

    assert replaced
    assert profile.endpoint == "https://verified.example.invalid"


def test_config_rejects_an_untrusted_parent_component(trusted_tmp_path: Path) -> None:
    parent = trusted_tmp_path / "untrusted"
    parent.mkdir()
    config = parent / "config.toml"
    config.write_text(
        '[profiles.default]\nendpoint = "https://safe.example.invalid"\n',
        encoding="utf-8",
    )
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(parent), "/grant", "*S-1-1-0:(M)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("Windows ACL modification is unavailable")
    else:
        parent.chmod(0o777)

    with pytest.raises(CliError, match="parent is untrusted") as raised:
        resolve_profile(profile_name=None, config_path=str(config), environ={})
    assert raised.value.exit_code == ExitCode.CONFIG


def test_invalid_endpoint_is_rejected_before_bearer_transport(tmp_path: Path) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid endpoint reached bearer transport: {request!r}")

    result = invoke_cli(
        ["capabilities"],
        handler=forbidden,
        tmp_path=tmp_path,
        environ={"PATCHOULI_ENDPOINT": "http://unsafe.example.invalid"},
    )

    assert result.status == ExitCode.CONFIG
    assert result.stdout == ""
    assert "HTTPS origin" in result.stderr


def test_nested_input_read_stays_bound_to_open_directory_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    moved = root / "opened-nested"
    nested.mkdir(parents=True)
    (nested / "payload.md").write_bytes(b"verified bytes")
    original_open_child = SecureDirectory.open_child
    replaced = False

    def replace_after_open(
        directory: SecureDirectory,
        name: str,
        *,
        create: bool = False,
        secure: bool = False,
    ) -> SecureDirectory:
        nonlocal replaced
        child = original_open_child(directory, name, create=create, secure=secure)
        if name == "nested" and not replaced:
            replaced = True
            nested.rename(moved)
            nested.mkdir()
            (nested / "payload.md").write_bytes(b"replacement bytes")
        return child

    monkeypatch.setattr(SecureDirectory, "open_child", replace_after_open)

    assert read_file("nested/payload.md", root=root, max_bytes=100) == b"verified bytes"
    assert replaced


def test_journal_stays_bound_to_open_directory_after_path_replacement(tmp_path: Path) -> None:
    state = tmp_path / "state"
    profile = state / "default"
    moved = state / "opened-default"
    with OperationJournal(state, "default") as journal:
        profile.rename(moved)
        profile.mkdir()
        record = journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=operation_fingerprint({"route": "synthetic"}),
            operation_id=None,
        )

    name = f"{record.operation_id}.json"
    assert (moved / name).is_file()
    assert not (profile / name).exists()


def test_journal_syncs_file_and_directory_around_create_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    original_write = SecureFile.write_all
    original_file_sync = SecureFile.sync
    original_directory_sync = SecureDirectory.sync
    original_replace = SecureDirectory.replace

    def write(file: SecureFile, data: bytes) -> None:
        events.append("write")
        original_write(file, data)

    def file_sync(file: SecureFile) -> None:
        events.append("file-sync")
        original_file_sync(file)

    def directory_sync(directory: SecureDirectory) -> None:
        events.append("directory-sync")
        original_directory_sync(directory)

    def replace(directory: SecureDirectory, source: str, target: str) -> None:
        events.append("replace")
        original_replace(directory, source, target)

    with OperationJournal(tmp_path / "state", "default") as journal:
        monkeypatch.setattr(SecureFile, "write_all", write)
        monkeypatch.setattr(SecureFile, "sync", file_sync)
        monkeypatch.setattr(SecureDirectory, "sync", directory_sync)
        monkeypatch.setattr(SecureDirectory, "replace", replace)
        record = journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=operation_fingerprint({"route": "synthetic"}),
            operation_id=None,
        )
        assert events == ["write", "file-sync", "directory-sync"]
        events.clear()
        journal.complete(record, request_id="req_synthetic")

    assert events == [
        "write",
        "file-sync",
        "directory-sync",
        "replace",
        "directory-sync",
    ]


def test_journal_directory_sync_failure_aborts_and_removes_pending_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original_sync = SecureDirectory.sync

    def fail_first_sync(directory: SecureDirectory) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic directory sync failure")
        original_sync(directory)

    with OperationJournal(tmp_path / "state", "default") as journal:
        monkeypatch.setattr(SecureDirectory, "sync", fail_first_sync)
        with pytest.raises(CliError, match="created durably"):
            journal.prepare(
                caller_id="caller_synthetic",
                kind="archive.create",
                fingerprint=operation_fingerprint({"route": "synthetic"}),
                operation_id=None,
            )

    assert calls == 2
    assert list((tmp_path / "state" / "default").glob("*.json")) == []


def test_new_journal_parent_chain_is_synced_and_sync_failure_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = cast(
        Any,
        importlib.import_module(
            "patchouli_cli._windows_fs" if os.name == "nt" else "patchouli_cli._posix_fs"
        ),
    )
    hook_name = "_flush_handle" if os.name == "nt" else "_sync_fd"
    original = cast(Callable[[int], None], getattr(backend, hook_name))
    calls: list[int] = []

    def record_sync(handle: int) -> None:
        calls.append(handle)
        original(handle)

    monkeypatch.setattr(backend, hook_name, record_sync)
    root = tmp_path / "new-a" / "new-b" / "state"
    with OperationJournal(root, "default"):
        pass
    assert len(calls) >= 8

    attempts = 0

    def fail_sync(handle: int) -> None:
        del handle
        nonlocal attempts
        attempts += 1
        raise OSError("synthetic parent sync failure")

    monkeypatch.setattr(backend, hook_name, fail_sync)
    with pytest.raises(CliError, match="could not be secured"):
        OperationJournal(tmp_path / "failure" / "state", "default")
    assert attempts == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX root safety")
def test_posix_root_never_chmods_an_existing_insecure_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = cast(Any, importlib.import_module("patchouli_cli._posix_fs"))
    directory = backend.PosixDirectory(123, created=False)
    info = os.stat_result((0o040755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    changed: list[tuple[int, int]] = []
    monkeypatch.setattr(backend, "_geteuid", lambda: 0)
    monkeypatch.setattr(backend.os, "fstat", lambda descriptor: info)
    monkeypatch.setattr(
        backend.os,
        "fchmod",
        lambda descriptor, mode: changed.append((descriptor, mode)),
    )

    with pytest.raises(PermissionError, match="root will not change"):
        directory._secure_current_user()
    assert changed == []


def test_journal_storage_is_current_user_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with OperationJournal(state, "default") as journal:
        record = journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=operation_fingerprint({"route": "synthetic"}),
            operation_id=None,
        )
        record_path = state / "default" / f"{record.operation_id}.json"
        assert current_user_only(state, directory=True)
        assert current_user_only(state / "default", directory=True)
        assert current_user_only(record_path, directory=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_journal_rejects_record_with_extra_principal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with OperationJournal(state, "default") as journal:
        record = journal.prepare(
            caller_id="caller_synthetic",
            kind="archive.create",
            fingerprint=operation_fingerprint({"route": "synthetic"}),
            operation_id=None,
        )
        path = state / "default" / f"{record.operation_id}.json"
        result = subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("Windows ACL modification is unavailable")
        assert not current_user_only(path, directory=False)
        with pytest.raises(CliError, match="secure regular non-reparse"):
            journal.prepare(
                caller_id="caller_synthetic",
                kind="archive.create",
                fingerprint=operation_fingerprint({"route": "synthetic"}),
                operation_id=record.operation_id,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_rejects_junction_traversal_for_inputs_and_journal(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    with pytest.raises(CliError, match="reparse"):
        open_input_root(str(link), {})
    with pytest.raises(CliError, match="directory could not be secured"):
        OperationJournal(link / "state", "default")
