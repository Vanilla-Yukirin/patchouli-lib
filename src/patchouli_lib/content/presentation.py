"""Explicit, immutable display metadata for one exact file-set Revision."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from markdown_it import MarkdownIt
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    Text,
    insert,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from patchouli_lib.content.file_manifest import FileManifest
from patchouli_lib.models import Base

UNTITLED = "无标题"


class RevisionPresentation(Base):
    __tablename__ = "revision_presentations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["library_id", "page_uid", "revision_id", "revision_number"],
            [
                "revisions.library_id",
                "revisions.page_uid",
                "revisions.revision_id",
                "revisions.revision_number",
            ],
            ondelete="RESTRICT",
            name="fk_revision_presentations_revision",
        ),
        CheckConstraint("length(display_title) >= 1", name="ck_revision_presentations_title"),
    )

    library_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    page_uid: Mapped[bytes] = mapped_column(LargeBinary(16), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    primary_file: Mapped[str | None] = mapped_column(Text)
    display_title: Mapped[str] = mapped_column(Text, nullable=False)


@dataclass(frozen=True, slots=True)
class Presentation:
    primary_file: str | None
    display_title: str


def preview_text(content: bytes) -> str | None:
    """Recognize source text; the browser separately bounds Markdown rendering.

    File-set validation already enforces the file and total package byte limits.
    A rendering budget must not make otherwise valid source uneditable.
    """
    if b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_title(filename: str | None, content: bytes | None) -> str:
    if (
        filename is None
        or content is None
        or PurePosixPath(filename).suffix.lower() not in {".md", ".markdown", ".mdown", ".mkd"}
    ):
        return UNTITLED
    text = preview_text(content)
    if text is None:
        return UNTITLED
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() in {"---", "..."}:
                text = "".join(lines[index + 1 :])
                break
    tokens = MarkdownIt("commonmark").parse(text)
    for index, token in enumerate(tokens[:-1]):
        if token.type != "heading_open" or token.tag != "h1":
            continue
        inline = tokens[index + 1]
        title = "".join(
            child.content if child.type not in {"softbreak", "hardbreak"} else " "
            for child in inline.children or []
            if child.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
        ).strip()
        return title or UNTITLED
    return UNTITLED


def presentation_for(manifest: FileManifest, primary_file: str | None) -> Presentation:
    if primary_file is None:
        return Presentation(None, UNTITLED)
    entry = next((entry for entry in manifest.files if entry.name == primary_file), None)
    if entry is None or preview_text(entry.content) is None:
        raise ValueError("The primary file must be an existing, safely readable text file.")
    return Presentation(primary_file, extract_title(primary_file, entry.content))


def read_presentation(
    connection: Connection, library_id: str, page_uid: bytes, revision_id: str
) -> Presentation:
    if not _available(connection):
        return Presentation(None, UNTITLED)
    row = connection.execute(
        select(RevisionPresentation.primary_file, RevisionPresentation.display_title).where(
            RevisionPresentation.library_id == library_id,
            RevisionPresentation.page_uid == page_uid,
            RevisionPresentation.revision_id == revision_id,
        )
    ).first()
    return Presentation(None, UNTITLED) if row is None else Presentation(row[0], row[1])


def save_presentation(
    connection: Connection,
    *,
    library_id: str,
    page_uid: bytes,
    revision_id: str,
    revision_number: int,
    presentation: Presentation,
) -> None:
    if not _available(connection):
        if presentation.primary_file is not None:
            raise ValueError("Explicit primary files require the current database schema.")
        return
    connection.execute(
        insert(RevisionPresentation).values(
            library_id=library_id,
            page_uid=page_uid,
            revision_id=revision_id,
            revision_number=revision_number,
            primary_file=presentation.primary_file,
            display_title=presentation.display_title,
        )
    )


def _available(connection: Connection) -> bool:
    # Historical backup fixtures still use their precise pre-feature schema.
    # This does not infer an entry file from old data.
    return (
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='revision_presentations'"
        ).scalar_one_or_none()
        is not None
    )
