from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _trusted_tmp_path


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_explicit_test_root_removes_only_its_own_child(tmp_path: Path, platform: str) -> None:
    parent = tmp_path / "configured-root"
    parent.mkdir()
    unrelated = parent / "unrelated.txt"
    unrelated.write_text("preserved", encoding="utf-8")

    with _trusted_tmp_path(
        tmp_path,
        environ={"PATCHOULI_TEST_TRUSTED_TMP_ROOT": str(parent)},
        platform=platform,
    ) as path:
        assert path.parent == parent
        assert path != parent
        assert path.is_dir()
        (path / "synthetic.txt").write_text("synthetic", encoding="utf-8")

    assert not path.exists()
    assert parent.is_dir()
    assert unrelated.read_text(encoding="utf-8") == "preserved"


@pytest.mark.parametrize("configured", ["", "relative-root"])
def test_explicit_test_root_rejects_nonabsolute_paths(tmp_path: Path, configured: str) -> None:
    with (
        pytest.raises(ValueError, match="existing absolute directory"),
        _trusted_tmp_path(
            tmp_path,
            environ={"PATCHOULI_TEST_TRUSTED_TMP_ROOT": configured},
            platform="nt",
        ),
    ):
        pytest.fail("invalid test root must not be selected")


@pytest.mark.parametrize("regular_file", [False, True])
def test_explicit_test_root_rejects_missing_or_nondirectory_paths(
    tmp_path: Path, regular_file: bool
) -> None:
    configured = tmp_path / "not-a-directory"
    if regular_file:
        configured.write_text("preserved", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="existing absolute directory"),
        _trusted_tmp_path(
            tmp_path,
            environ={"PATCHOULI_TEST_TRUSTED_TMP_ROOT": str(configured)},
            platform="posix",
        ),
    ):
        pytest.fail("invalid test root must not be selected")
    assert configured.is_file() == regular_file


def test_posix_default_retains_pytest_directory(tmp_path: Path) -> None:
    with _trusted_tmp_path(tmp_path, environ={}, platform="posix") as path:
        assert path == tmp_path
    assert tmp_path.is_dir()


def test_windows_default_retains_localappdata_parent(tmp_path: Path) -> None:
    with _trusted_tmp_path(
        tmp_path, environ={"LOCALAPPDATA": str(tmp_path)}, platform="nt"
    ) as path:
        parent = tmp_path / "PatchouliLibTests"
        assert path.parent == parent
        assert path.is_dir()
    assert not path.exists()
    assert parent.is_dir()


def test_windows_default_skips_without_localappdata(tmp_path: Path) -> None:
    with (
        pytest.raises(pytest.skip.Exception, match="LOCALAPPDATA is unavailable"),
        _trusted_tmp_path(tmp_path, environ={}, platform="nt"),
    ):
        pytest.fail("missing platform test root must be skipped")
