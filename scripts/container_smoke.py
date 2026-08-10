from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def wait_until_ready(port: int, timeout_seconds: int = 45) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/health/ready"
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                payload = json.load(response)
            if payload == {"status": "ready"}:
                return
        except Exception as exc:  # noqa: BLE001 - retain the last smoke-test failure
            last_error = exc
        time.sleep(1)

    message = f"Container did not become ready within {timeout_seconds}s: {last_error}"
    raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="patchouli-lib:validation")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    suffix = str(os.getpid())
    container_name = f"patchouli-validation-{suffix}"
    volume_name = f"patchouli-validation-data-{suffix}"

    if not args.skip_build:
        run("docker", "build", "--tag", args.image, ".")

    run("docker", "volume", "create", volume_name)
    try:
        run(
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--publish",
            "127.0.0.1::8000",
            "--mount",
            f"type=volume,source={volume_name},target=/data",
            args.image,
        )
        port_output = run("docker", "port", container_name, "8000/tcp", capture=True)
        port = int(port_output.rsplit(":", maxsplit=1)[1])
        wait_until_ready(port)
        print(f"Container smoke test passed on an ephemeral loopback port ({port}).")
    except Exception:
        subprocess.run(
            ["docker", "logs", container_name],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        raise
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "volume", "rm", "--force", volume_name],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
        )


if __name__ == "__main__":
    main()
