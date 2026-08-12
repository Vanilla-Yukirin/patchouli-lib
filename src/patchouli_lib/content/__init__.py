"""Persistence contracts for Library-scoped Page and Revision content."""

from patchouli_lib.content.schemas import (
    MarkdownContent,
    NewPage,
    NewPageIdCollisionCounter,
    NewPageIdentifier,
    NewPageSource,
    NewRevision,
    PageIdCollisionCounterRecord,
    PageIdentifierRecord,
    PageRecord,
    PageSourceRecord,
    RevisionRecord,
)

__all__ = [
    "MarkdownContent",
    "NewPage",
    "NewPageIdCollisionCounter",
    "NewPageIdentifier",
    "NewPageSource",
    "NewRevision",
    "PageIdCollisionCounterRecord",
    "PageIdentifierRecord",
    "PageRecord",
    "PageSourceRecord",
    "RevisionRecord",
]
