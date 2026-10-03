from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

WIRE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "api" / "agent_v1_wire.json"
)


@contextmanager
def _trusted_tmp_path(
    tmp_path: Path, *, environ: Mapping[str, str], platform: str
) -> Iterator[Path]:
    configured = environ.get("PATCHOULI_TEST_TRUSTED_TMP_ROOT")
    if configured is not None:
        parent = Path(configured)
        if not configured or not parent.is_absolute() or not parent.is_dir():
            raise ValueError(
                "PATCHOULI_TEST_TRUSTED_TMP_ROOT must be an existing absolute directory"
            )
    elif platform != "nt":
        yield tmp_path
        return
    else:
        local = environ.get("LOCALAPPDATA")
        if not local:
            pytest.skip("LOCALAPPDATA is unavailable")
        parent = Path(local) / "PatchouliLibTests"
        parent.mkdir(exist_ok=True)

    path = parent / str(uuid.uuid4())
    path.mkdir()
    try:
        yield path
    finally:
        # Remove only the fresh child owned by this fixture, never the configured root.
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def trusted_tmp_path(tmp_path: Path) -> Iterator[Path]:
    """Select an isolated test directory without bypassing product filesystem checks."""
    with _trusted_tmp_path(tmp_path, environ=os.environ, platform=os.name) as path:
        yield path


def load_agent_wire_fixture() -> dict[str, object]:
    return cast(dict[str, object], json.loads(WIRE_FIXTURE_PATH.read_text(encoding="utf-8")))


def protected_headers(**extra: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "private, no-store",
        "X-Request-ID": "req_synthetic",
        **extra,
    }


def sample_page(
    *,
    content: str | None = "# Synthetic archive",
    revision_number: int = 1,
    revision_id: str = "rev_0123456789abcdef0123456789abcdef",
    current_revision_number: int | None = None,
    current_revision_id: str | None = None,
) -> dict[str, object]:
    current_number = revision_number if current_revision_number is None else current_revision_number
    current_id = revision_id if current_revision_id is None else current_revision_id
    revision: dict[str, object] = {
        "page_id": "20260811t091500123z-synthetic-session",
        "revision_id": revision_id,
        "revision_number": revision_number,
        "created_at": "2026-08-11T09:16:00.000000Z",
        "content_type": "text/markdown;charset=utf-8",
        "content_sha256": "a" * 64,
        "future_revision_field": {"safe": True},
    }
    if content is not None:
        revision["content"] = content
    page: Mapping[str, object] = {
        "section_id": "sec_synthetic",
        "book_id": "book_synthetic",
        "page_id": "20260811t091500123z-synthetic-session",
        "title": "Synthetic session",
        "type": "archive",
        "occurred_at": "2026-08-11T09:15:00.123456Z",
        "current_revision_id": current_id,
        "current_revision_number": current_number,
        "future_page_field": "ignored",
    }
    citation: Mapping[str, object] = {
        "section_id": "sec_synthetic",
        "page_id": "20260811t091500123z-synthetic-session",
        "revision_id": revision_id,
        "revision_number": revision_number,
        "href": (
            "/api/v1/sections/sec_synthetic/pages/"
            f"20260811t091500123z-synthetic-session/revisions/{revision_number}"
        ),
        "future_citation_field": 1,
    }
    return {
        "page": page,
        "revision": revision,
        "citation": citation,
        "future_document_field": ["ignored"],
    }
