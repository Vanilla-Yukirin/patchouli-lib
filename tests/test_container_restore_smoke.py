from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import container_restore_smoke as smoke  # noqa: E402
from container_restore_smoke import (  # noqa: E402
    CommandResult,
    DockerClient,
    SmokeFailure,
    SmokeInconclusive,
    _subprocess_environment,
    _subprocess_runner,
    run_docker_smoke,
)

RUN_ID = "patchouli-smoke-test-0123456789ab"


class FakeRunner:
    def __init__(
        self,
        failures: dict[tuple[str, ...], int] | None = None,
        *,
        docker_available: bool = True,
        context_available: bool = True,
        exceptions: dict[tuple[str, ...], BaseException] | None = None,
        present_after_failure: set[tuple[str, ...]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self._failures = failures or {}
        self._docker_available = docker_available
        self._context_available = context_available
        self._exceptions = exceptions or {}
        self._present_after_failure = present_after_failure or set()
        self._present: set[tuple[str, str]] = set()
        self._identities: dict[tuple[str, str], str] = {}
        self._labels: dict[tuple[str, str], str] = {}
        self.endpoint = "unix:///var/run/docker.sock"

    def __call__(
        self,
        arguments: Sequence[str],
        timeout_seconds: float,
        environment: object,
    ) -> CommandResult:
        del environment
        call = tuple(arguments)
        self.calls.append(call)
        self.timeouts.append(timeout_seconds)
        normalized = call[3:]
        if normalized[:3] == ("context", "inspect", "default"):
            if not self._context_available:
                return CommandResult(1)
            return CommandResult(0, f"{self.endpoint}\n".encode())
        if normalized == ("version",):
            return CommandResult(0 if self._docker_available else 1)
        for suffix, exception in self._exceptions.items():
            if call[-len(suffix) :] == suffix:
                if suffix in self._present_after_failure:
                    self._record_created(normalized)
                raise exception
        for suffix, result in self._failures.items():
            if call[-len(suffix) :] == suffix:
                if suffix in self._present_after_failure:
                    self._record_created(normalized)
                return CommandResult(result)
        if len(normalized) >= 2 and normalized[1] == "inspect":
            kind = normalized[0]
            name = normalized[-1]
            key = self._resolve(kind, name)
            if "--format" not in normalized:
                return CommandResult(0 if key is not None else 1)
            if key is None:
                return CommandResult(1)
            identity = self._identities.get(key, key[1])
            label = self._labels.get(key, RUN_ID)
            return CommandResult(0, f"{identity}|{label}\n".encode())
        if self._record_created(normalized):
            return CommandResult(0)
        if len(normalized) >= 2 and normalized[1] == "rm":
            key = self._resolve(normalized[0], normalized[-1])
            if key is not None:
                self._present.remove(key)
        return CommandResult(0)

    def _record_created(self, normalized: tuple[str, ...]) -> bool:
        kind: str
        name: str
        if normalized[:1] == ("build",):
            kind = "image"
            name = normalized[normalized.index("--tag") + 1]
        elif normalized[:2] == ("volume", "create"):
            kind = "volume"
            name = normalized[-1]
        elif normalized[:2] == ("container", "create"):
            kind = "container"
            name = normalized[normalized.index("--name") + 1]
        else:
            return False
        key = (kind, name)
        self._present.add(key)
        if kind in {"container", "image"}:
            self._identities[key] = f"owned-{kind}-{name}"
        return True

    def _resolve(self, kind: str, name: str) -> tuple[str, str] | None:
        direct = (kind, name)
        if direct in self._present:
            return direct
        return next(
            (
                key
                for key, identity in self._identities.items()
                if key[0] == kind and identity == name
            ),
            None,
        )


def _client(runner: FakeRunner) -> DockerClient:
    return DockerClient("docker", 30.0, runner)


def _create_calls(runner: FakeRunner) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for call in runner.calls:
        if call[3:5] == ("container", "create"):
            result[call[call.index("--name") + 1].rsplit("-", maxsplit=1)[-1]] = call
    return result


def test_smoke_uses_distinct_volumes_and_cleans_every_resource() -> None:
    runner = FakeRunner()

    run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    creates = _create_calls(runner)
    source_volume = f"{RUN_ID}-source"
    backup_volume = f"{RUN_ID}-backup"
    destination_volume = f"{RUN_ID}-destination"
    assert any(source_volume in argument for argument in creates["source"])
    assert any(backup_volume in argument for argument in creates["source"])
    assert all(destination_volume not in argument for argument in creates["source"])
    for phase in ("restore", "verify"):
        assert any(destination_volume in argument for argument in creates[phase])
        assert any(backup_volume in argument for argument in creates[phase])
        assert all(source_volume not in argument for argument in creates[phase])
        assert "none" in creates[phase]
        assert "--read-only" in creates[phase]
        assert any(
            "target=/app/container_restore_smoke.py,readonly" in argument
            for argument in creates[phase]
        )
    source_removed = runner.calls.index(
        (
            "docker",
            "--context",
            "default",
            "container",
            "rm",
            "--force",
            f"owned-container-{RUN_ID}-source",
        )
    )
    destination_created = runner.calls.index(
        (
            "docker",
            "--context",
            "default",
            "volume",
            "create",
            "--label",
            f"org.patchouli.smoke={RUN_ID}",
            destination_volume,
        )
    )
    assert source_removed < destination_created
    assert ("docker", "--context", "default", "volume", "rm", destination_volume) in runner.calls
    assert ("docker", "--context", "default", "volume", "rm", backup_volume) in runner.calls
    assert ("docker", "--context", "default", "volume", "rm", source_volume) in runner.calls
    assert any("image" in call and "rm" in call for call in runner.calls)
    cleanup_indexes = [
        index
        for index, call in enumerate(runner.calls)
        if call[3:5] in {("container", "rm"), ("volume", "rm"), ("image", "rm")}
    ]
    assert cleanup_indexes
    assert all(runner.timeouts[index] == 30.0 for index in cleanup_indexes)


def test_dockerfile_has_fixed_non_root_identity_and_portable_directories() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "groupadd --gid 10001 patchouli" in dockerfile
    assert "useradd --uid 10001 --gid 10001" in dockerfile
    assert "install -d -o 10001 -g 10001 /app /data /backups" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "chown -R" not in dockerfile


def test_internal_phases_round_trip_exact_semantic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    backups = tmp_path / "backups"
    source.mkdir()
    destination.mkdir()
    backups.mkdir()
    source_database = source / "patchouli.db"
    destination_database = destination / "patchouli.db"
    monkeypatch.setattr(smoke, "_runtime_identity", lambda: None)
    monkeypatch.setattr(smoke, "DATABASE_PATH", source_database)
    monkeypatch.setattr(smoke, "BACKUP_ROOT", backups)
    monkeypatch.setattr(smoke, "BUNDLE_PATH", backups / "bundle")
    monkeypatch.setattr(smoke, "EXPECTED_PATH", backups / "expected.json")
    monkeypatch.setattr(smoke, "SOURCE_MARKER", source / "source-volume-only")
    monkeypatch.setattr(smoke, "DESTINATION_MARKER", source / "restored-destination-only")

    smoke._seed_backup()

    monkeypatch.setattr(smoke, "DATABASE_PATH", destination_database)
    monkeypatch.setattr(smoke, "SOURCE_MARKER", destination / "source-volume-only")
    monkeypatch.setattr(
        smoke,
        "DESTINATION_MARKER",
        destination / "restored-destination-only",
    )
    smoke._restore()
    smoke._verify()

    assert source_database.is_file()
    assert destination_database.is_file()
    assert source_database.resolve() != destination_database.resolve()
    assert (source / "source-volume-only").is_file()
    assert not (destination / "source-volume-only").exists()
    assert (destination / "restored-destination-only").is_file()


def test_command_failure_triggers_bounded_cleanup() -> None:
    source_name = f"{RUN_ID}-source"
    source_identity = f"owned-container-{source_name}"
    runner = FakeRunner({("start", "--attach", source_identity): 7})

    with pytest.raises(SmokeFailure, match="container phase failed"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert (
        "docker",
        "--context",
        "default",
        "container",
        "rm",
        "--force",
        source_identity,
    ) in runner.calls
    assert (
        "docker",
        "--context",
        "default",
        "volume",
        "rm",
        f"{RUN_ID}-backup",
    ) in runner.calls
    assert (
        "docker",
        "--context",
        "default",
        "volume",
        "rm",
        f"{RUN_ID}-source",
    ) in runner.calls
    assert any("image" in call and "rm" in call for call in runner.calls)


def test_failed_resource_creation_never_deletes_an_unowned_name() -> None:
    source_volume = f"{RUN_ID}-source"
    runner = FakeRunner(
        {("volume", "create", "--label", f"org.patchouli.smoke={RUN_ID}", source_volume): 7}
    )

    with pytest.raises(SmokeFailure, match="cleanup could not be verified"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert all(
        not ("volume" in call and "rm" in call and source_volume in call) for call in runner.calls
    )
    assert any("image" in call and "rm" in call for call in runner.calls)


def test_created_resource_is_not_ledgered_until_label_ownership_is_verified() -> None:
    source_volume = f"{RUN_ID}-source"
    runner = FakeRunner()
    runner._labels[("volume", source_volume)] = "foreign"

    with pytest.raises(SmokeFailure, match="cleanup could not be verified"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert any("volume" in call and "create" in call for call in runner.calls)
    assert all(
        not ("volume" in call and "rm" in call and source_volume in call) for call in runner.calls
    )


def test_cleanup_failure_changes_an_otherwise_successful_run_to_failure() -> None:
    image_identity = f"owned-image-{RUN_ID}:local"
    runner = FakeRunner({("image", "rm", "--force", image_identity): 9})

    with pytest.raises(SmokeFailure, match="cleanup failed"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)


def test_cleanup_continues_after_container_remove_raises() -> None:
    source_identity = f"owned-container-{RUN_ID}-source"
    runner = FakeRunner(
        exceptions={
            ("container", "rm", "--force", source_identity): SmokeFailure("redacted"),
        }
    )

    with pytest.raises(SmokeFailure, match="cleanup also failed"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert ("docker", "--context", "default", "volume", "rm", f"{RUN_ID}-backup") in runner.calls
    assert ("docker", "--context", "default", "volume", "rm", f"{RUN_ID}-source") in runner.calls
    assert any(call[3:6] == ("image", "rm", "--force") for call in runner.calls)


def test_cleanup_continues_past_exception_and_refuses_foreign_resource() -> None:
    runner = FakeRunner()
    client = _client(runner)
    names = {
        "container": f"{RUN_ID}-source",
        "foreign": f"{RUN_ID}-foreign",
        "volume": f"{RUN_ID}-backup",
        "image": f"{RUN_ID}:local",
    }
    for kind, name in (
        ("container", names["container"]),
        ("volume", names["foreign"]),
        ("volume", names["volume"]),
        ("image", names["image"]),
    ):
        runner._present.add((kind, name))
        if kind in {"container", "image"}:
            runner._identities[(kind, name)] = f"owned-{kind}-{name}"
    resources = {
        key: client.owned_resource("volume" if key in {"foreign", "volume"} else key, name, RUN_ID)
        for key, name in names.items()
    }
    runner._labels[("volume", names["foreign"])] = "foreign"
    runner._exceptions[
        (
            "container",
            "rm",
            "--force",
            resources["container"].identity,
        )
    ] = OSError("synthetic")
    ledger = smoke.ResourceLedger(
        client,
        containers=[resources["container"]],
        volumes=[resources["volume"], resources["foreign"]],
        images=[resources["image"]],
    )

    with pytest.raises(SmokeFailure, match="cleanup failed"):
        ledger.cleanup()

    assert ("docker", "--context", "default", "volume", "rm", names["volume"]) in runner.calls
    assert any(call[3:6] == ("image", "rm", "--force") for call in runner.calls)
    assert all(
        not (call[3:5] == ("volume", "rm") and names["foreign"] in call) for call in runner.calls
    )


def test_ambiguous_create_timeout_cleans_only_exact_labeled_resource() -> None:
    source_volume = f"{RUN_ID}-source"
    suffix = (
        "volume",
        "create",
        "--label",
        f"org.patchouli.smoke={RUN_ID}",
        source_volume,
    )
    runner = FakeRunner(
        exceptions={suffix: subprocess.TimeoutExpired("docker", 1)},
        present_after_failure={suffix},
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert ("docker", "--context", "default", "volume", "rm", source_volume) in runner.calls


def test_ambiguous_build_timeout_cleans_only_exact_labeled_image() -> None:
    image = f"{RUN_ID}:local"
    suffix = ("--tag", image, str(REPOSITORY_ROOT.resolve()))
    runner = FakeRunner(
        exceptions={suffix: subprocess.TimeoutExpired("docker", 1)},
        present_after_failure={suffix},
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert any(
        call[3:6] == ("image", "rm", "--force") and f"owned-image-{image}" in call
        for call in runner.calls
    )


def test_ambiguous_create_failure_never_deletes_foreign_collision() -> None:
    source_volume = f"{RUN_ID}-source"
    suffix = (
        "volume",
        "create",
        "--label",
        f"org.patchouli.smoke={RUN_ID}",
        source_volume,
    )
    runner = FakeRunner({suffix: 7}, present_after_failure={suffix})
    runner._labels[("volume", source_volume)] = "foreign"

    with pytest.raises(SmokeFailure, match="cleanup could not be verified"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert all(
        not (call[3:5] == ("volume", "rm") and source_volume in call) for call in runner.calls
    )


def test_ambiguous_build_failure_never_deletes_foreign_collision() -> None:
    image = f"{RUN_ID}:local"
    suffix = ("--tag", image, str(REPOSITORY_ROOT.resolve()))
    runner = FakeRunner({suffix: 7}, present_after_failure={suffix})
    runner._labels[("image", image)] = "foreign"

    with pytest.raises(SmokeFailure, match="cleanup could not be verified"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert all(call[3:6] != ("image", "rm", "--force") for call in runner.calls)


def test_unavailable_docker_is_inconclusive_without_creating_resources() -> None:
    runner = FakeRunner(docker_available=False)

    with pytest.raises(SmokeInconclusive, match="unavailable"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert runner.calls[0][3:6] == ("context", "inspect", "default")
    assert runner.calls[1] == ("docker", "--context", "default", "version")


def test_unavailable_default_context_is_inconclusive_without_creating_resources() -> None:
    runner = FakeRunner(context_available=False)

    with pytest.raises(SmokeInconclusive, match="context is unavailable"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert len(runner.calls) == 1
    assert runner.calls[0][3:6] == ("context", "inspect", "default")


def test_collision_and_malformed_run_identifier_fail_closed() -> None:
    collision = FakeRunner({("image", "inspect", f"{RUN_ID}:local"): 0})
    with pytest.raises(SmokeFailure, match="collided"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(collision), run_id=RUN_ID)
    with pytest.raises(SmokeFailure, match="identifier"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(FakeRunner()), run_id="../unsafe")


def test_environment_drops_proxy_and_docker_redirect_values() -> None:
    environment = _subprocess_environment(
        {
            "PATH": "synthetic-path",
            "TEMP": "synthetic-temp",
            "HTTP_PROXY": "http://invalid.example",
            "https_proxy": "http://invalid.example",
            "DOCKER_HOST": "tcp://invalid.example:2375",
            "DOCKER_CONTEXT": "remote",
            "DOCKER_CONFIG": "remote-config",
            "HOME": "synthetic-home",
            "USERPROFILE": "synthetic-profile",
            "UNRELATED_SECRET": "do-not-forward",
        },
        docker_config=Path("synthetic-docker-config"),
    )

    assert environment == {
        "DOCKER_CONFIG": "synthetic-docker-config",
        "PATH": "synthetic-path",
        "TEMP": "synthetic-temp",
    }


def test_cleanup_refuses_resource_when_verified_ownership_changes() -> None:
    runner = FakeRunner()
    client = _client(runner)
    runner._present.add(("volume", f"{RUN_ID}-source"))
    resource = client.owned_resource("volume", f"{RUN_ID}-source", RUN_ID)
    ledger = smoke.ResourceLedger(client, volumes=[resource])
    runner._labels[("volume", resource.identity)] = "foreign"

    with pytest.raises(SmokeFailure, match="cleanup failed"):
        ledger.cleanup()

    assert all(not ("volume" in call and "rm" in call) for call in runner.calls)


def test_remote_default_endpoint_fails_before_creating_resources() -> None:
    runner = FakeRunner()
    runner.endpoint = "tcp://invalid.example:2375"

    with pytest.raises(SmokeFailure, match="not local"):
        run_docker_smoke(REPOSITORY_ROOT, docker=_client(runner), run_id=RUN_ID)

    assert len(runner.calls) == 1
    assert runner.calls[0][3:6] == ("context", "inspect", "default")


def test_restore_rejects_any_unexpected_destination_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "unexpected").write_text("synthetic", encoding="ascii")
    monkeypatch.setattr(smoke, "_runtime_identity", lambda: None)
    monkeypatch.setattr(smoke, "DATABASE_PATH", destination / "patchouli.db")

    with pytest.raises(SmokeFailure, match="contents are not exact"):
        smoke._restore()


def test_minimum_semantics_rejects_self_consistent_empty_state() -> None:
    with pytest.raises(SmokeFailure, match="incomplete"):
        smoke._require_minimum_semantics(
            {
                "counts": {},
                "pages": [],
                "revisions": [],
                "sources": [],
                "audit_actions": [],
                "auth_relationships": [],
                "idempotency_relationships": [],
                "citations": [],
            }
        )


def test_subprocess_timeout_is_failure_without_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("docker", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SmokeFailure, match="could not complete"):
        _subprocess_runner(("docker", "version"), 1.0, {})


def test_actual_fresh_volume_restore_when_docker_is_available() -> None:
    try:
        run_docker_smoke(
            REPOSITORY_ROOT,
            docker=DockerClient("docker", 300.0),
        )
    except SmokeInconclusive as exc:
        pytest.skip(str(exc))
