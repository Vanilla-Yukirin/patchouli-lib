from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def run_controller(tmp_path: Path, image: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PATCHOULI_DEPLOY_ROOT": str(tmp_path),
            "PATCHOULI_IMAGE_REPOSITORY": "ghcr.io/example/patchouli-lib",
            "SSH_ORIGINAL_COMMAND": image,
        }
    )
    return subprocess.run(
        [find_posix_shell(), "deploy/remote-deploy.sh"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )


def test_controller_rejects_wrong_repository(tmp_path: Path) -> None:
    result = run_controller(
        tmp_path,
        "ghcr.io/example/another-image@sha256:" + ("a" * 64),
    )

    assert result.returncode == 64
    assert "Deployment rejected" in result.stderr


def test_controller_rejects_invalid_digest(tmp_path: Path) -> None:
    result = run_controller(
        tmp_path,
        "ghcr.io/example/patchouli-lib@sha256:not-a-digest",
    )

    assert result.returncode == 64
    assert "invalid sha256" in result.stderr


def test_controller_requires_private_runtime_configuration(tmp_path: Path) -> None:
    result = run_controller(
        tmp_path,
        "ghcr.io/example/patchouli-lib@sha256:" + ("a" * 64),
    )

    assert result.returncode == 78
    assert "configuration is unavailable" in result.stderr
