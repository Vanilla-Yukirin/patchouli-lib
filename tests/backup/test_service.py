from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from patchouli_lib.backup import (
    BACKUP_FILENAME,
    MANIFEST_FILENAME,
    BackupArtifactIdentity,
    BackupCancelledError,
    BackupConfigurationError,
    BackupDatabaseError,
    BackupManifestError,
    BackupOperationError,
    BackupResult,
    BackupTimeoutError,
    create_backup,
    restore_backup,
    validate_database,
    verify_backup_bundle,
)
from patchouli_lib.backup import service as backup_service
from patchouli_lib.backup.manifest import MAX_MANIFEST_BYTES

from .conftest import APP_VERSION


def identity() -> BackupArtifactIdentity:
    return BackupArtifactIdentity("synthetic/source@immutable", "sha256:" + "1" * 64)


def _create(engine: Engine, path: Path) -> BackupResult:
    return create_backup(
        engine,
        path,
        artifact_identity=identity(),
        app_version=APP_VERSION,
    )


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_live_backup_and_restore_preserve_complete_domain_state(
    complete_engine: Engine,
    tmp_path: Path,
    journal_mode: str,
) -> None:
    with complete_engine.connect() as connection:
        observed = connection.exec_driver_sql(f"PRAGMA journal_mode = {journal_mode}").scalar_one()
    assert observed == journal_mode

    bundle = tmp_path / f"bundle-{journal_mode}"
    result = _create(complete_engine, bundle)

    assert result.manifest.source_journal_mode == journal_mode
    assert result.manifest.artifact_identity == "synthetic/source@immutable"
    assert result.manifest.artifact_digest == "sha256:" + "1" * 64
    assert {item.name for item in bundle.iterdir()} == {BACKUP_FILENAME, MANIFEST_FILENAME}
    assert not any(Path(f"{result.database_path}{suffix}").exists() for suffix in ("-wal", "-shm"))
    assert verify_backup_bundle(bundle, app_version=APP_VERSION) == result.manifest

    restored = tmp_path / f"restored-{journal_mode}.sqlite"
    restore_result = restore_backup(bundle, restored, app_version=APP_VERSION)
    assert restore_result.destination_path == restored
    validate_database(restored)

    with closing(sqlite3.connect(restored)) as sqlite_connection:
        assert sqlite_connection.execute("SELECT count(*) FROM auth_callers").fetchone() == (2,)
        assert sqlite_connection.execute("SELECT count(*) FROM auth_credentials").fetchone() == (3,)
        assert sqlite_connection.execute("SELECT count(*) FROM auth_section_grants").fetchone() == (
            1,
        )
        assert sqlite_connection.execute("SELECT count(*) FROM auth_audit_events").fetchone() == (
            1,
        )
        assert sqlite_connection.execute("SELECT count(*) FROM pages").fetchone() == (1,)
        assert sqlite_connection.execute("SELECT count(*) FROM revisions").fetchone() == (1,)
        assert sqlite_connection.execute("SELECT count(*) FROM page_sources").fetchone() == (1,)
        assert sqlite_connection.execute("SELECT count(*) FROM idempotency_records").fetchone() == (
            1,
        )
        assert sqlite_connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260813_0006",
        )
        assert sqlite_connection.execute(
            "SELECT revision_id, revision_number, locator FROM page_sources"
        ).fetchone() == ("rev_" + "22" * 16, 1, "urn:synthetic:archive")
        replay = sqlite_connection.execute(
            "SELECT response_body FROM idempotency_records"
        ).fetchone()[0]
        assert b'"citation"' in replay
        assert sqlite_connection.execute(
            "SELECT revoked_at, rotated_at, rotated_to_credential_id "
            "FROM auth_credentials WHERE id = ?",
            ("d" * 32,),
        ).fetchone() == (20, 20, "e" * 32)


def test_online_backup_observes_one_consistent_concurrent_snapshot(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    with complete_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode = WAL")

    started = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        sequence = 20
        try:
            while not stop.is_set():
                with complete_engine.begin() as connection:
                    sequence = 21 if sequence == 20 else 20
                    connection.execute(
                        text(
                            "UPDATE auth_credentials SET last_used_at = :value, "
                            "updated_at = :value WHERE id = :credential"
                        ),
                        {"value": sequence, "credential": "e" * 32},
                    )
                started.set()
        except BaseException as exc:  # pragma: no cover - reported in main thread
            failures.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert started.wait(timeout=5)
    try:
        result = _create(complete_engine, tmp_path / "concurrent-bundle")
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []

    with closing(sqlite3.connect(result.database_path)) as sqlite_connection:
        last_used_at, updated_at = sqlite_connection.execute(
            "SELECT last_used_at, updated_at FROM auth_credentials WHERE id = ?",
            ("e" * 32,),
        ).fetchone()
    assert last_used_at == updated_at
    assert last_used_at in {20, 21}


def test_configuration_rejects_memory_relative_and_existing_destinations(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    memory_engine = create_engine("sqlite:///:memory:")
    relative_engine = create_engine("sqlite:///relative.sqlite")
    missing_path = tmp_path / "missing.sqlite"
    ambiguous_engine = create_engine(f"sqlite:///{missing_path.as_posix()}?uri=true")
    missing_engine = create_engine(f"sqlite:///{missing_path.as_posix()}")
    existing = tmp_path / "existing"
    existing.mkdir()
    try:
        for engine in (memory_engine, relative_engine, ambiguous_engine, missing_engine):
            with pytest.raises(BackupConfigurationError):
                _create(engine, tmp_path / f"bad-{id(engine)}")
        with pytest.raises(BackupConfigurationError):
            _create(complete_engine, existing)
        with pytest.raises(BackupConfigurationError):
            _create(complete_engine, Path("relative-bundle"))
    finally:
        memory_engine.dispose()
        relative_engine.dispose()
        ambiguous_engine.dispose()
        missing_engine.dispose()


def test_paths_refuse_overwrite_and_internal_hardlinks(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    result = _create(complete_engine, bundle)
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(b"existing")
    with pytest.raises(BackupConfigurationError):
        restore_backup(bundle, restored, app_version=APP_VERSION)
    assert restored.read_bytes() == b"existing"

    external_alias = tmp_path / "database-alias.sqlite"
    os.link(result.database_path, external_alias)
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)


def test_source_hardlink_alias_is_rejected(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    source = Path(str(complete_engine.url.database))
    alias = tmp_path / "source-alias.sqlite"
    os.link(source, alias)
    alias_engine = create_engine(f"sqlite:///{alias.as_posix()}")
    try:
        with pytest.raises(BackupConfigurationError):
            _create(alias_engine, tmp_path / "alias-bundle")
    finally:
        alias_engine.dispose()


def test_source_parent_directory_symlink_is_rejected(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    source = Path(str(complete_engine.url.database))
    linked_parent = tmp_path / "source-parent-link"
    try:
        linked_parent.symlink_to(source.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform.")
    bundle = tmp_path / "symlink-source-bundle"
    alias_engine = create_engine(f"sqlite:///{(linked_parent / source.name).as_posix()}")
    try:
        with pytest.raises(BackupConfigurationError):
            _create(alias_engine, bundle)
        assert not bundle.exists()
    finally:
        alias_engine.dispose()
        linked_parent.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression.")
def test_source_parent_directory_junction_is_rejected(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    source = Path(str(complete_engine.url.database))
    junction = tmp_path / "source-parent-junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(source.parent)],
        check=True,
        capture_output=True,
    )
    bundle = tmp_path / "junction-source-bundle"
    alias_engine = create_engine(f"sqlite:///{(junction / source.name).as_posix()}")
    try:
        with pytest.raises(BackupConfigurationError):
            _create(alias_engine, bundle)
        assert not bundle.exists()
    finally:
        alias_engine.dispose()
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression.")
def test_source_parent_swap_to_junction_is_rejected_before_backup(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoritative = Path(str(complete_engine.url.database))
    configured_parent = tmp_path / "configured-source"
    alternate_parent = tmp_path / "alternate-source"
    configured_parent.mkdir()
    alternate_parent.mkdir()
    filename = "database.sqlite"

    def clone(destination: Path) -> None:
        with (
            closing(sqlite3.connect(authoritative)) as source_connection,
            closing(sqlite3.connect(destination)) as destination_connection,
        ):
            source_connection.backup(destination_connection)

    configured_database = configured_parent / filename
    alternate_database = alternate_parent / filename
    clone(configured_database)
    clone(alternate_database)
    with closing(sqlite3.connect(alternate_database)) as alternate_connection:
        alternate_connection.execute(
            "UPDATE auth_credentials SET last_used_at = 777, updated_at = 777 WHERE id = ?",
            ("e" * 32,),
        )
        alternate_connection.commit()
    validate_database(configured_database)
    validate_database(alternate_database)

    source_engine = create_engine(f"sqlite:///{configured_database.as_posix()}")
    displaced_parent = tmp_path / "displaced-source"
    real_engine_source = backup_service._engine_source
    real_sqlite_backup = backup_service._sqlite_backup
    backup_called = False
    swapped = False

    def swap_after_initial_check(engine: Engine) -> Any:
        nonlocal swapped
        binding = real_engine_source(engine)
        configured_parent.rename(displaced_parent)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(configured_parent), str(alternate_parent)],
            check=True,
            capture_output=True,
        )
        swapped = True
        return binding

    def observe_backup(*args: Any, **kwargs: Any) -> None:
        nonlocal backup_called
        backup_called = True
        real_sqlite_backup(*args, **kwargs)

    monkeypatch.setattr(backup_service, "_engine_source", swap_after_initial_check)
    monkeypatch.setattr(backup_service, "_sqlite_backup", observe_backup)
    bundle = tmp_path / "swapped-source-bundle"
    try:
        with pytest.raises(BackupConfigurationError):
            _create(source_engine, bundle)
        assert not bundle.exists()
        assert not backup_called
    finally:
        source_engine.dispose()
        if swapped:
            configured_parent.rmdir()
            displaced_parent.rename(configured_parent)


def test_bundle_file_symlink_or_reparse_is_rejected(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    result = _create(complete_engine, bundle)
    original_manifest = tmp_path / "original-manifest.json"
    result.manifest_path.replace(original_manifest)

    try:
        result.manifest_path.symlink_to(original_manifest)
    except OSError:
        original_manifest.replace(result.manifest_path)
        pytest.skip("File symlinks are unavailable on this platform.")
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)


def test_manifest_checksum_truncation_and_compatibility_fail_closed(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    result = _create(complete_engine, bundle)

    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version="0.1.0a1")

    original_manifest = result.manifest_path.read_bytes()
    result.manifest_path.write_bytes(original_manifest.replace(b'"byte_size":', b'"bad_size":'))
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)
    result.manifest_path.write_bytes(original_manifest)

    with result.database_path.open("r+b") as handle:
        handle.truncate(max(1, result.manifest.byte_size // 2))
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(bundle, app_version=APP_VERSION)


def test_bundle_io_rejects_oversized_manifest_extra_entries_and_recursion(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = tmp_path / "oversized"
    oversized_result = _create(complete_engine, oversized)
    oversized_result.manifest_path.write_bytes(b"{" + b" " * MAX_MANIFEST_BYTES)
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(oversized, app_version=APP_VERSION)

    extra = tmp_path / "extra"
    _create(complete_engine, extra)
    (extra / "unexpected").write_bytes(b"synthetic")
    with pytest.raises(BackupManifestError):
        verify_backup_bundle(extra, app_version=APP_VERSION)

    def recurse(_path: Path, *, must_exist: bool) -> Path:
        del must_exist
        raise RecursionError

    monkeypatch.setattr(backup_service, "_canonical_path", recurse)
    with pytest.raises(BackupManifestError, match="Backup manifest validation failed"):
        verify_backup_bundle(extra, app_version=APP_VERSION)


def test_restore_rejects_nested_destination_without_modifying_bundle(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    result = _create(complete_engine, bundle)
    before = {
        BACKUP_FILENAME: result.database_path.read_bytes(),
        MANIFEST_FILENAME: result.manifest_path.read_bytes(),
    }

    for target in (bundle, bundle / "nested.sqlite"):
        with pytest.raises(BackupConfigurationError):
            restore_backup(bundle, target, app_version=APP_VERSION)

    assert {item.name for item in bundle.iterdir()} == set(before)
    assert result.database_path.read_bytes() == before[BACKUP_FILENAME]
    assert result.manifest_path.read_bytes() == before[MANIFEST_FILENAME]


def test_restore_rejects_different_valid_staged_bytes(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    _create(complete_engine, bundle)
    real_copy = backup_service._copy_verified_file

    def mutate_staged_copy(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> None:
        real_copy(source, destination, **kwargs)
        with closing(sqlite3.connect(destination)) as writable:
            writable.execute("PRAGMA user_version = 1")
            writable.commit()

    monkeypatch.setattr(backup_service, "_copy_verified_file", mutate_staged_copy)
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(BackupDatabaseError):
        restore_backup(bundle, restored, app_version=APP_VERSION)
    assert not restored.exists()


def test_restore_detects_bundle_change_after_copy(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    _create(complete_engine, bundle)
    real_copy = backup_service._copy_verified_file

    def mutate_source_after_copy(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> None:
        real_copy(source, destination, **kwargs)
        with closing(sqlite3.connect(source)) as writable:
            writable.execute("PRAGMA user_version = 1")
            writable.commit()

    monkeypatch.setattr(backup_service, "_copy_verified_file", mutate_source_after_copy)
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(BackupManifestError):
        restore_backup(bundle, restored, app_version=APP_VERSION)
    assert not restored.exists()


def test_cancel_and_publication_failure_remove_all_temporary_artifacts(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = tmp_path / "cancelled"
    with pytest.raises(BackupCancelledError):
        create_backup(
            complete_engine,
            cancelled,
            artifact_identity=identity(),
            app_version=APP_VERSION,
            pages_per_step=1,
            cancel_check=lambda: True,
        )
    assert not cancelled.exists()
    assert list(tmp_path.glob(".cancelled.staging-*")) == []

    original_link = os.link
    calls = 0

    def fail_manifest_link(source: Any, destination: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publication interruption")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", fail_manifest_link)
    interrupted = tmp_path / "interrupted"
    with pytest.raises(BackupOperationError):
        _create(complete_engine, interrupted)
    assert not interrupted.exists()
    assert list(tmp_path.glob(".interrupted.staging-*")) == []
    assert "not crash-durable" in (backup_service._publish_bundle.__doc__ or "")


def test_cleanup_refuses_replaced_staging_directory_without_traversal(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced_root: Path | None = None
    renamed_root: Path | None = None

    def replace_staging(
        staging: Path,
        _staging_identity: tuple[int, int],
        _destination: Path,
    ) -> tuple[int, int]:
        nonlocal replaced_root, renamed_root
        renamed_root = staging.with_name(f"{staging.name}.renamed")
        staging.rename(renamed_root)
        staging.mkdir()
        replaced_root = staging
        nested = staging / "attacker-tree"
        nested.mkdir()
        (nested / "sentinel").write_text("preserve", encoding="utf-8")
        raise BackupOperationError

    monkeypatch.setattr(backup_service, "_publish_bundle", replace_staging)
    with pytest.raises(BackupOperationError):
        _create(complete_engine, tmp_path / "replacement")

    assert replaced_root is not None
    assert renamed_root is not None
    assert (replaced_root / "attacker-tree" / "sentinel").read_text(encoding="utf-8") == (
        "preserve"
    )
    assert (renamed_root / BACKUP_FILENAME).is_file()
    assert (renamed_root / MANIFEST_FILENAME).is_file()


def test_restore_cancellation_leaves_no_destination_or_temp(
    complete_engine: Engine,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    _create(complete_engine, bundle)
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(BackupCancelledError):
        restore_backup(
            bundle,
            restored,
            app_version=APP_VERSION,
            pages_per_step=1,
            cancel_check=lambda: True,
        )
    assert not restored.exists()
    assert list(tmp_path.glob(".restored.sqlite.restore-*")) == []


def test_restore_timeout_is_bounded_and_cleans_up(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    _create(complete_engine, bundle)
    ticks = iter((0.0, 2.0, 2.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 2.0))
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(BackupTimeoutError):
        restore_backup(
            bundle,
            restored,
            app_version=APP_VERSION,
            operation_timeout_seconds=1.0,
        )
    assert not restored.exists()
    assert list(tmp_path.glob(".restored.sqlite.restore-*")) == []


def test_backup_timeout_is_bounded_and_cleans_up(
    complete_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((0.0, 2.0, 2.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 2.0))
    bundle = tmp_path / "timed-out"
    with pytest.raises(BackupTimeoutError):
        create_backup(
            complete_engine,
            bundle,
            artifact_identity=identity(),
            app_version=APP_VERSION,
            pages_per_step=1,
            operation_timeout_seconds=1.0,
        )
    assert not bundle.exists()
    assert list(tmp_path.glob(".timed-out.staging-*")) == []
