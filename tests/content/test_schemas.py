import hashlib

import pytest
from pydantic import ValidationError

from patchouli_lib.content.models import EXHAUSTED_COLLISION_ORDINAL, MAX_MARKDOWN_BYTES
from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    PageRecord,
)
from patchouli_lib.identifiers import PAGE_ID_SCHEME, generate_page_id, page_id_registry_digest
from patchouli_lib.identifiers.page_ids import parse_occurrence_time

LIBRARY_ID = "1" * 32
SECTION_ID = "2" * 32
BOOK_ID = "3" * 32
PAGE_UID = b"\x11" * 16
REVISION_ID = "rev_" + "22" * 16


def valid_page_values() -> dict[str, object]:
    occurrence = parse_occurrence_time("1969-12-31T23:59:59.999999Z")
    generated = generate_page_id(occurrence, "Synthetic Archive")
    return {
        "library_id": LIBRARY_ID,
        "page_uid": PAGE_UID,
        "section_id": SECTION_ID,
        "book_id": BOOK_ID,
        "page_id": generated.value,
        "id_scheme": PAGE_ID_SCHEME,
        "id_timestamp_micros": -1_000,
        "base_slug": generated.base_slug,
        "collision_ordinal": 1,
        "title": "Synthetic Archive",
        "page_type": "archive",
        "occurred_at": -1,
        "current_revision_id": REVISION_ID,
        "current_revision_number": 1,
        "created_at": 1_000_000,
        "updated_at": 1_000_000,
    }


def valid_source_values() -> dict[str, object]:
    return {
        "library_id": LIBRARY_ID,
        "source_id": "3" * 32,
        "page_uid": PAGE_UID,
        "revision_id": REVISION_ID,
        "revision_number": 1,
        "kind": "synthetic",
        "created_at": 1_000_000,
    }


def test_markdown_content_preserves_exact_utf8_bytes_and_metadata() -> None:
    raw = "# 合成\r\n\r\nCafe\u0301\n".encode()

    content = MarkdownContent.from_bytes(raw)

    assert content.content_md is raw
    assert content.content_size_bytes == len(raw)
    assert content.content_sha256 == hashlib.sha256(raw).digest()
    assert content.model_dump()["content_md"] == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"synthetic\x00content",
        b"\xff",
        b"x" * (MAX_MARKDOWN_BYTES + 1),
    ],
    ids=["empty", "nul", "invalid-utf8", "oversize"],
)
def test_markdown_content_rejects_invalid_storage_bytes_without_echo(raw: bytes) -> None:
    with pytest.raises((ValidationError, ValueError)) as exc_info:
        MarkdownContent.from_bytes(raw)

    assert "synthetic" not in str(exc_info.value)


def test_markdown_content_requires_exact_bytes_type() -> None:
    with pytest.raises(ValueError, match="exact bytes"):
        MarkdownContent.from_bytes(bytearray(b"synthetic"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "replacement",
    [
        {"content_size_bytes": 1},
        {"content_sha256": b"\x00" * 32},
    ],
)
def test_markdown_content_rejects_mismatched_metadata(replacement: dict[str, object]) -> None:
    raw = b"synthetic"
    values: dict[str, object] = {
        "content_md": raw,
        "content_size_bytes": len(raw),
        "content_sha256": hashlib.sha256(raw).digest(),
    }
    values.update(replacement)

    with pytest.raises(ValidationError):
        MarkdownContent.model_validate(values)


def test_page_schema_validates_components_without_parsing_the_opaque_id() -> None:
    page = NewPage.model_validate(valid_page_values())

    assert page.page_id == "19691231t235959999z-synthetic-archive"
    assert page.occurred_at == -1
    assert page.id_timestamp_micros == -1_000


def test_stored_page_record_allows_title_change_without_recomputing_identity() -> None:
    values = valid_page_values()
    values.update({"title": "Updated Synthetic Title", "updated_at": 2_000_000})

    record = PageRecord.model_validate(values)

    assert record.title == "Updated Synthetic Title"
    assert record.page_id == "19691231t235959999z-synthetic-archive"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_id", "19691231t235959999z-other"),
        ("base_slug", "other"),
        ("id_timestamp_micros", 0),
        ("collision_ordinal", 2),
        ("id_scheme", "page-v2"),
        ("title", "Synthetic\x00Archive"),
        ("page_type", " archive "),
        ("current_revision_id", "rev_" + "A" * 32),
        ("current_revision_number", 2),
        ("page_uid", b"short"),
        ("updated_at", 999_999),
        ("deleted_at", 2_000_000),
    ],
)
def test_page_schema_rejects_inconsistent_or_malformed_storage(
    field: str,
    value: object,
) -> None:
    values = valid_page_values()
    values[field] = value

    with pytest.raises(ValidationError):
        NewPage.model_validate(values)


def test_revision_schema_validates_identity_number_and_exact_content() -> None:
    content = MarkdownContent.from_bytes(b"# Synthetic\n")
    revision = NewRevision(
        library_id=LIBRARY_ID,
        revision_id=REVISION_ID,
        page_uid=PAGE_UID,
        revision_number=1,
        created_at=1_000_000,
        **content.model_dump(),
    )

    assert revision.revision_number == 1
    assert revision.content_md == b"# Synthetic\n"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_id", "rev_" + "A" * 32),
        ("revision_number", 0),
        ("revision_number", True),
        ("page_uid", bytearray(PAGE_UID)),
        ("created_at", -1),
    ],
)
def test_revision_schema_rejects_malformed_storage(field: str, value: object) -> None:
    content = MarkdownContent.from_bytes(b"synthetic")
    values: dict[str, object] = {
        "library_id": LIBRARY_ID,
        "revision_id": REVISION_ID,
        "page_uid": PAGE_UID,
        "revision_number": 1,
        "created_at": 1_000_000,
        **content.model_dump(),
    }
    values[field] = value

    with pytest.raises(ValidationError):
        NewRevision.model_validate(values)


def test_identifier_schema_requires_exact_domain_separated_digest() -> None:
    page_id = str(valid_page_values()["page_id"])
    valid = {
        "library_id": LIBRARY_ID,
        "identifier_digest": page_id_registry_digest(page_id),
        "identifier_text": page_id,
        "id_scheme": PAGE_ID_SCHEME,
        "identifier_kind": "canonical",
        "page_uid": PAGE_UID,
        "created_at": 1_000_000,
    }

    assert NewPageIdentifier.model_validate(valid).identifier_text == page_id
    with pytest.raises(ValidationError):
        NewPageIdentifier.model_validate({**valid, "identifier_digest": b"\x00" * 32})


@pytest.mark.parametrize("next_ordinal", [2, EXHAUSTED_COLLISION_ORDINAL])
def test_counter_schema_accepts_active_and_exhausted_states(next_ordinal: int) -> None:
    counter = NewPageIdCollisionCounter(
        library_id=LIBRARY_ID,
        id_scheme=PAGE_ID_SCHEME,
        id_timestamp_micros=-1_000,
        base_slug="synthetic-archive",
        next_ordinal=next_ordinal,
    )
    assert counter.next_ordinal == next_ordinal


@pytest.mark.parametrize(
    ("timestamp", "next_ordinal"),
    [(-1, 2), (-1_000, 1), (-1_000, EXHAUSTED_COLLISION_ORDINAL + 1)],
)
def test_counter_schema_rejects_invalid_floor_or_ordinal(
    timestamp: int,
    next_ordinal: int,
) -> None:
    with pytest.raises(ValidationError):
        NewPageIdCollisionCounter(
            library_id=LIBRARY_ID,
            id_scheme=PAGE_ID_SCHEME,
            id_timestamp_micros=timestamp,
            base_slug="synthetic-archive",
            next_ordinal=next_ordinal,
        )


def test_source_schema_keeps_locator_optional_and_does_not_define_identity() -> None:
    first = NewPageSource.model_validate({**valid_source_values(), "locator": None})
    second = NewPageSource(
        library_id=LIBRARY_ID,
        source_id="4" * 32,
        page_uid=PAGE_UID,
        revision_id=REVISION_ID,
        revision_number=1,
        kind="synthetic import",
        locator="urn:synthetic:shared",
        captured_at=-1,
        created_at=1_000_000,
    )

    assert first.locator is None
    assert second.locator == "urn:synthetic:shared"
    assert second.revision_id == REVISION_ID
    assert second.revision_number == 1


@pytest.mark.parametrize(
    ("kind", "locator"),
    [("", None), (" synthetic ", None), ("synthetic\x00kind", None), ("synthetic", "")],
)
def test_source_schema_rejects_invalid_text(kind: str, locator: str | None) -> None:
    with pytest.raises(ValidationError):
        NewPageSource.model_validate(
            {
                **valid_source_values(),
                "kind": kind,
                "locator": locator,
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision_id", "rev_" + "A" * 32),
        ("revision_number", 0),
        ("revision_number", True),
    ],
)
def test_source_schema_requires_exact_revision_identity(field: str, value: object) -> None:
    values = valid_source_values()
    values[field] = value

    with pytest.raises(ValidationError):
        NewPageSource.model_validate(values)
