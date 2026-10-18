"""Local pytest artifacts must not be present in redistributable archives."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_VERIFY = Path(__file__).resolve().parents[1] / "scripts" / "verify_artifacts.py"
_METADATA = b"Metadata-Version: 2.4\nName: synthetic-client\nLicense-File: LICENSE\n\n"


def _write_artifacts(directory: Path, *, wheel_extra: str = "", sdist_extra: str = "") -> None:
    with zipfile.ZipFile(directory / "synthetic_client-0.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr("synthetic_client-0.0.0.dist-info/METADATA", _METADATA)
        archive.writestr("synthetic_client-0.0.0.dist-info/licenses/LICENSE", b"MIT test fixture")
        archive.writestr("synthetic_client/__init__.py", b"")
        if wheel_extra:
            archive.writestr(wheel_extra, b"synthetic local test output")
    with tarfile.open(directory / "synthetic_client-0.0.0.tar.gz", "w:gz") as archive:
        files = {
            "PKG-INFO": _METADATA,
            "LICENSE": b"MIT test fixture",
            "src/synthetic_client/__init__.py": b"",
        }
        if sdist_extra:
            files[sdist_extra] = b"synthetic local test output"
        for name, content in files.items():
            member = tarfile.TarInfo("synthetic_client-0.0.0/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def _verify(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_VERIFY), str(directory)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_clean_artifacts_keep_installable_sources_and_license(tmp_path: Path) -> None:
    _write_artifacts(tmp_path)
    result = _verify(tmp_path)
    assert result.returncode == 0 and not result.stdout and not result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("name", [".tmp-test/receipt.json", "nested/.tmp-test/output.json"])
def test_local_test_paths_are_rejected(tmp_path: Path, kind: str, name: str) -> None:
    _write_artifacts(
        tmp_path,
        wheel_extra=name if kind == "wheel" else "",
        sdist_extra=name if kind == "sdist" else "",
    )
    result = _verify(tmp_path)
    assert result.returncode != 0
    assert f"{kind} contained local test artifacts" in result.stderr


def test_ordinary_tmp_names_are_not_rejected(tmp_path: Path) -> None:
    _write_artifacts(tmp_path, wheel_extra="synthetic_client/tmp.py", sdist_extra="tests/tmp.py")
    assert _verify(tmp_path).returncode == 0
