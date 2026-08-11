from __future__ import annotations

import argparse
import tarfile
import zipfile
from collections.abc import Sequence
from email.parser import BytesParser
from pathlib import Path


def _single_artifact(directory: Path, pattern: str) -> Path:
    matches = tuple(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one artifact matching {pattern!r}")
    return matches[0]


def _license_declared(metadata: bytes, *, artifact: str) -> None:
    message = BytesParser().parsebytes(metadata)
    declared = message.get_all("License-File", [])
    if "LICENSE" not in declared:
        raise ValueError(f"{artifact} metadata did not declare License-File: LICENSE")


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        license_names = [name for name in names if name.endswith(".dist-info/licenses/LICENSE")]
        if len(metadata_names) != 1 or len(license_names) != 1:
            raise ValueError("wheel did not contain one metadata file and one MIT license file")
        _license_declared(archive.read(metadata_names[0]), artifact="wheel")


def verify_sdist(path: Path) -> None:
    with tarfile.open(path, mode="r:gz") as archive:
        names = archive.getnames()
        metadata_names = [name for name in names if name.endswith("/PKG-INFO")]
        license_names = [name for name in names if name.endswith("/LICENSE")]
        if len(metadata_names) != 1 or len(license_names) != 1:
            raise ValueError("sdist did not contain one metadata file and one MIT license file")
        metadata = archive.extractfile(metadata_names[0])
        if metadata is None:  # pragma: no cover - tar member was just enumerated
            raise ValueError("sdist metadata could not be read")
        _license_declared(metadata.read(), artifact="sdist")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify license metadata in built artifacts.")
    parser.add_argument("directory", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)

    verify_wheel(_single_artifact(args.directory, "*.whl"))
    verify_sdist(_single_artifact(args.directory, "*.tar.gz"))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by build validation
    raise SystemExit(main())
