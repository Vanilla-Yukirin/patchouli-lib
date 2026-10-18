"""Public wire contracts for protected Tag browsing and Page associations."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from patchouli_lib.api.contracts import WireModel
from patchouli_lib.library.schemas import OpaqueId


class CreateTagInput(WireModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]


class TagDefinitionView(WireModel):
    tag_id: OpaqueId
    name: str
    created_at: int


class TagView(TagDefinitionView):
    page_count: int


class TagCollection(WireModel):
    items: list[TagView]
    next_offset: int | None


class PageTagCollection(WireModel):
    items: list[TagDefinitionView]
    next_offset: int | None


class TaggedPageView(WireModel):
    section_id: OpaqueId
    page_id: str
    title: str
    occurred_at: int


class TaggedPageCollection(WireModel):
    items: list[TaggedPageView]
    next_offset: int | None


class TagAssociationResult(WireModel):
    changed: bool


__all__ = [
    "CreateTagInput",
    "PageTagCollection",
    "TagAssociationResult",
    "TagCollection",
    "TagDefinitionView",
    "TagView",
    "TaggedPageCollection",
    "TaggedPageView",
]
