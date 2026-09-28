"""Transaction-neutral Tag persistence; API authorization belongs to callers."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass

from sqlalchemy import Connection, delete, insert, select

from patchouli_lib.tags.models import PageTag, Tag


@dataclass(frozen=True, slots=True)
class TagRecord:
    library_id: str
    id: str
    display_name: str
    match_key: str
    created_at: int


@dataclass(frozen=True, slots=True)
class PageTagRecord:
    library_id: str
    page_uid: bytes
    tag_id: str
    created_at: int


def normalize_tag_name(name: str) -> tuple[str, str]:
    """Return an NFC display name and NFC(casefold(NFC(name))) matching key."""

    if type(name) is not str:
        raise ValueError("Tag name must be text.")
    display_name = unicodedata.normalize("NFC", name)
    match_key = unicodedata.normalize("NFC", display_name.casefold())
    if (
        not display_name
        or display_name != display_name.strip()
        or not match_key
        or match_key != match_key.strip()
        or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in display_name)
        or len(display_name) > 100
        or len(display_name.encode("utf-8")) > 255
        or len(match_key) > 100
        or len(match_key.encode("utf-8")) > 255
    ):
        raise ValueError("Tag name is invalid or too long.")
    return display_name, match_key


def _require_id(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 32
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError("Tag or Library ID must be 32 lowercase hex characters.")


class TagRepository:
    """Persist definitions and associations without committing or granting access."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add_tag(self, *, library_id: str, tag_id: str, name: str, created_at: int) -> TagRecord:
        _require_id(library_id)
        _require_id(tag_id)
        if type(created_at) is not int or created_at < 0:
            raise ValueError("Tag creation time must be nonnegative microseconds.")
        display_name, match_key = normalize_tag_name(name)
        record = TagRecord(library_id, tag_id, display_name, match_key, created_at)
        self._connection.execute(insert(Tag), asdict(record))
        return record

    def get_tag(self, *, library_id: str, tag_id: str) -> TagRecord | None:
        row = (
            self._connection.execute(
                select(Tag.__table__).where(Tag.library_id == library_id, Tag.id == tag_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else TagRecord(**row)

    def find_tag(self, *, library_id: str, name: str) -> TagRecord | None:
        _, match_key = normalize_tag_name(name)
        row = (
            self._connection.execute(
                select(Tag.__table__).where(
                    Tag.library_id == library_id, Tag.match_key == match_key
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else TagRecord(**row)

    def list_tags(self, *, library_id: str) -> list[TagRecord]:
        rows = self._connection.execute(
            select(Tag.__table__)
            .where(Tag.library_id == library_id)
            .order_by(Tag.match_key, Tag.id)
        ).mappings()
        return [TagRecord(**row) for row in rows]

    def attach_page(
        self, *, library_id: str, page_uid: bytes, tag_id: str, created_at: int
    ) -> PageTagRecord:
        _require_id(library_id)
        _require_id(tag_id)
        if type(page_uid) is not bytes or len(page_uid) != 16:
            raise ValueError("Page UID must be 16 bytes.")
        if type(created_at) is not int or created_at < 0:
            raise ValueError("Tag association time must be nonnegative microseconds.")
        record = PageTagRecord(library_id, page_uid, tag_id, created_at)
        self._connection.execute(insert(PageTag), asdict(record))
        return record

    def detach_page(self, *, library_id: str, page_uid: bytes, tag_id: str) -> bool:
        result = self._connection.execute(
            delete(PageTag).where(
                PageTag.library_id == library_id,
                PageTag.page_uid == page_uid,
                PageTag.tag_id == tag_id,
            )
        )
        return result.rowcount == 1

    def list_page_tags(self, *, library_id: str, page_uid: bytes) -> list[TagRecord]:
        rows = self._connection.execute(
            select(Tag.__table__)
            .join(
                PageTag,
                (PageTag.library_id == Tag.library_id) & (PageTag.tag_id == Tag.id),
            )
            .where(PageTag.library_id == library_id, PageTag.page_uid == page_uid)
            .order_by(Tag.match_key, Tag.id)
        ).mappings()
        return [TagRecord(**row) for row in rows]

    def list_tag_pages(self, *, library_id: str, tag_id: str) -> list[PageTagRecord]:
        rows = self._connection.execute(
            select(PageTag.__table__)
            .where(PageTag.library_id == library_id, PageTag.tag_id == tag_id)
            .order_by(PageTag.created_at, PageTag.page_uid)
        ).mappings()
        return [PageTagRecord(**row) for row in rows]
