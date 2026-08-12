from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

WIRE_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "api" / "agent_v1_wire.json"
)


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
