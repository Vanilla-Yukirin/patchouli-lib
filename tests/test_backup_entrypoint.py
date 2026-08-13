from __future__ import annotations

import subprocess
import sys
from importlib.metadata import EntryPoint, entry_points


def _belongs_to_server_distribution(entry_point: EntryPoint) -> bool:
    distribution = entry_point.dist
    return distribution is not None and distribution.name == "patchouli-lib"


def test_distribution_exposes_backup_console_entrypoint() -> None:
    candidates = [
        entry_point
        for entry_point in entry_points(group="console_scripts", name="patchouli-backup")
        if _belongs_to_server_distribution(entry_point)
    ]

    assert len(candidates) == 1
    assert candidates[0].value == "patchouli_lib.backup_cli:main"


def test_backup_module_is_callable() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "patchouli_lib.backup_cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "patchouli-backup" in result.stdout
    assert "create" in result.stdout
    assert "verify" in result.stdout
    assert "restore" in result.stdout
    assert result.stderr == ""
