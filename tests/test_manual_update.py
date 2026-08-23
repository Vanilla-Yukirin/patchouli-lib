from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
IMAGE_REPOSITORY = "ghcr.io/example/patchouli-lib"
VALID_IMAGE = IMAGE_REPOSITORY + "@sha256:" + ("a" * 64)


def find_posix_shell() -> str:
    shell = shutil.which("sh")
    if shell is not None:
        return shell

    git = shutil.which("git")
    if git is not None:
        candidate = Path(git).resolve().parents[1] / "bin" / "sh.exe"
        if candidate.is_file():
            return str(candidate)

    pytest.skip("POSIX shell is unavailable")


def shell_path(path: Path) -> str:
    return path.as_posix()


def prepare_runtime(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "runtime.env").write_text("EXAMPLE=value\n", encoding="utf-8")

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_directory / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$*" in\n'
        '  *"${FAKE_DOCKER_FAIL_ON:-__never__}"*) exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_docker.chmod(0o755)
    return bin_directory, docker_log


def run_update(
    tmp_path: Path,
    *arguments: str,
    fake_docker_failure: str | None = None,
    deploy_root: str | None = None,
    ssh_original_command: str | None = None,
    working_directory: Path = REPOSITORY_ROOT,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATCHOULI_DEPLOY_ROOT": deploy_root or shell_path(tmp_path),
            "PATCHOULI_IMAGE_REPOSITORY": IMAGE_REPOSITORY,
        }
    )
    if ssh_original_command is None:
        environment.pop("SSH_ORIGINAL_COMMAND", None)
    else:
        environment["SSH_ORIGINAL_COMMAND"] = ssh_original_command

    bin_directory = tmp_path / "bin"
    if bin_directory.is_dir():
        bin_path = shell_path(bin_directory)
        inherited_path = environment.get("PATH", "")
        environment["PATH"] = f"{bin_path}:{inherited_path}"
        environment["FAKE_DOCKER_LOG"] = shell_path(tmp_path / "docker.log")
    if fake_docker_failure is not None:
        environment["FAKE_DOCKER_FAIL_ON"] = fake_docker_failure

    return subprocess.run(
        [
            find_posix_shell(),
            shell_path(REPOSITORY_ROOT / "deploy" / "manual-update.sh"),
            *arguments,
        ],
        cwd=working_directory,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )


def test_manual_update_requires_exactly_one_explicit_image(tmp_path: Path) -> None:
    no_image = run_update(tmp_path)
    too_many = run_update(tmp_path, VALID_IMAGE, VALID_IMAGE)

    assert no_image.returncode == 64
    assert too_many.returncode == 64
    assert "Usage: manual-update.sh" in no_image.stderr


def test_manual_update_ignores_ssh_original_command(tmp_path: Path) -> None:
    prepare_runtime(tmp_path)

    result = run_update(
        tmp_path,
        VALID_IMAGE,
        ssh_original_command="ghcr.io/example/another-image@sha256:" + ("b" * 64),
    )

    assert result.returncode == 0
    assert (tmp_path / "current-image").read_text(encoding="utf-8") == (VALID_IMAGE + "\n")


def test_manual_update_rejects_wrong_repository(tmp_path: Path) -> None:
    result = run_update(
        tmp_path,
        "ghcr.io/example/another-image@sha256:" + ("a" * 64),
    )

    assert result.returncode == 64
    assert "Manual update rejected" in result.stderr


@pytest.mark.parametrize(
    "digest",
    [
        "not-a-digest",
        "A" * 64,
        "a" * 63,
        "a" * 65,
        ("a" * 64) + "\nignored",
    ],
)
def test_manual_update_rejects_noncanonical_digest(
    tmp_path: Path,
    digest: str,
) -> None:
    result = run_update(
        tmp_path,
        IMAGE_REPOSITORY + "@sha256:" + digest,
    )

    assert result.returncode == 64
    if "\n" not in digest:
        assert "invalid sha256" in result.stderr


def test_manual_update_requires_local_runtime_configuration(tmp_path: Path) -> None:
    result = run_update(tmp_path, VALID_IMAGE)

    assert result.returncode == 78
    assert "configuration is unavailable" in result.stderr


def test_manual_update_validates_pulls_starts_and_records_exact_image(
    tmp_path: Path,
) -> None:
    _, docker_log = prepare_runtime(tmp_path)

    result = run_update(tmp_path, VALID_IMAGE)

    assert result.returncode == 0
    assert result.stdout == "Manual update completed and health checks passed.\n"
    assert result.stderr == ""
    assert (tmp_path / "current-image").read_text(encoding="utf-8") == (VALID_IMAGE + "\n")
    commands = docker_log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 3
    assert commands[0].endswith("config --quiet")
    assert commands[1].endswith("pull api")
    assert commands[2].endswith("up --detach --no-build --wait --wait-timeout 90 api")


def test_manual_update_resolves_relative_deploy_root_before_local_paths(
    tmp_path: Path,
) -> None:
    prepare_runtime(tmp_path)

    result = run_update(
        tmp_path,
        VALID_IMAGE,
        deploy_root=tmp_path.name,
        working_directory=tmp_path.parent,
    )

    assert result.returncode == 0
    assert (tmp_path / "current-image").read_text(encoding="utf-8") == VALID_IMAGE + "\n"


@pytest.mark.parametrize(
    ("failure", "expected_returncode", "expected_message", "command_count"),
    [
        (
            "config --quiet",
            78,
            "failed while validating local Compose configuration",
            1,
        ),
        ("pull api", 1, "failed while pulling the requested image", 2),
        ("up --detach", 1, "failed health checks", 3),
    ],
)
def test_manual_update_fails_closed_without_changing_recorded_image(
    tmp_path: Path,
    failure: str,
    expected_returncode: int,
    expected_message: str,
    command_count: int,
) -> None:
    _, docker_log = prepare_runtime(tmp_path)
    previous_image = IMAGE_REPOSITORY + "@sha256:" + ("b" * 64)
    (tmp_path / "current-image").write_text(
        previous_image + "\n",
        encoding="utf-8",
    )

    result = run_update(
        tmp_path,
        VALID_IMAGE,
        fake_docker_failure=failure,
    )

    assert result.returncode == expected_returncode
    assert expected_message in result.stderr
    assert (tmp_path / "current-image").read_text(encoding="utf-8") == (previous_image + "\n")
    commands = docker_log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == command_count
    assert all("stop api" not in command for command in commands)
    assert all(previous_image not in command for command in commands)

    if failure == "up --detach":
        assert "Automatic image rollback is disabled" in result.stderr
