from __future__ import annotations

import subprocess
import sys


def test_module_is_callable_without_claiming_console_script_registration() -> None:
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
