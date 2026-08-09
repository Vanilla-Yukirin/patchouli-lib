from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(args)}", flush=True)
    executable = shutil.which(args[0])
    if executable is None:
        message = f"Required command is not available: {args[0]}"
        raise RuntimeError(message)
    subprocess.run((executable, *args[1:]), cwd=REPOSITORY_ROOT, env=env, check=True)


def find_posix_shell() -> str:
    shell = shutil.which("sh")
    if shell is not None:
        return shell

    git = shutil.which("git")
    if git is not None:
        candidate = Path(git).resolve().parents[1] / "bin" / "sh.exe"
        if candidate.is_file():
            return str(candidate)

    raise RuntimeError("A POSIX shell is required to validate deployment scripts.")


def validate_migrations() -> None:
    with tempfile.TemporaryDirectory(prefix="patchouli-migration-") as temporary_directory:
        database_path = (Path(temporary_directory) / "migration.db").as_posix()
        environment = os.environ.copy()
        environment["PATCHOULI_DATABASE_URL"] = f"sqlite:///{database_path}"
        environment["PATCHOULI_ENVIRONMENT"] = "test"
        run("uv", "run", "alembic", "upgrade", "head", env=environment)
        run("uv", "run", "alembic", "downgrade", "base", env=environment)
        run("uv", "run", "alembic", "upgrade", "head", env=environment)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the public repository.")
    parser.add_argument(
        "--container",
        action="store_true",
        help="Also build the OCI image and run a loopback health smoke test.",
    )
    parser.add_argument(
        "--skip-docs",
        action="store_true",
        help="Skip Node.js installation and public Markdown validation.",
    )
    args = parser.parse_args()

    run("uv", "sync", "--frozen", "--all-groups")
    run("uv", "run", "ruff", "format", "--check", ".")
    run("uv", "run", "ruff", "check", ".")
    run("uv", "run", "mypy", "src", "tests", "scripts")
    run("uv", "run", "pytest")
    run(
        find_posix_shell(),
        "-n",
        "docker/entrypoint.sh",
        "scripts/validate.sh",
        "deploy/remote-deploy.sh",
    )
    validate_migrations()
    if not args.skip_docs:
        run("npm", "ci")
        run("npm", "run", "lint:docs")

    if args.container:
        run("docker", "compose", "config", "--quiet")
        run("uv", "run", "python", "scripts/container_smoke.py")
        run(
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{REPOSITORY_ROOT}:/repo",
            "--workdir",
            "/repo",
            "rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667",
        )


if __name__ == "__main__":
    main()
