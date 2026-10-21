"""Read-only management labels for revisions predating explicit presentation.

Keep this separate from mutation/no-op and restore metadata. A derived entry is
never persisted merely because a document or its directory was read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath

from sqlalchemy import Connection, select, tuple_

from patchouli_lib.content.file_manifest import FileManifest, ManifestFile
from patchouli_lib.content.models import Page, Revision, RevisionFile
from patchouli_lib.content.presentation import (
    UNTITLED,
    Presentation,
    RevisionPresentation,
    extract_title,
    preview_text,
)

RevisionKey = tuple[str, bytes, str]
_MARKDOWN = {".md", ".markdown", ".mdown", ".mkd"}


def legacy_entry(names: Iterable[str]) -> str | None:
    """Choose only a unique Markdown or an unambiguous README, never an attachment."""
    names = list(names)
    markdown = [name for name in names if PurePosixPath(name).suffix.lower() in _MARKDOWN]
    if len(markdown) == 1:
        return markdown[0]
    readmes = [name for name in markdown if PurePosixPath(name).stem.casefold() == "readme"]
    if not markdown:
        readmes = [
            name for name in names if name.casefold() in {"readme", "readme.txt", "readme.html"}
        ]
    return readmes[0] if len(readmes) == 1 else None


def _derived(entry: ManifestFile | None, legacy_title: str) -> Presentation:
    title = legacy_title.strip() or (entry.name if entry is not None else UNTITLED)
    if entry is None or preview_text(entry.content) is None:
        return Presentation(None, title)
    heading = extract_title(entry.name, entry.content)
    return Presentation(entry.name, title if heading == UNTITLED else heading)


def legacy_presentation(manifest: FileManifest, legacy_title: str) -> Presentation:
    primary = legacy_entry(entry.name for entry in manifest.files)
    entry = next((entry for entry in manifest.files if entry.name == primary), None)
    return _derived(entry, legacy_title)


def resolve_presentations(
    connection: Connection,
    revisions: Iterable[RevisionKey],
    *,
    manifests: Mapping[RevisionKey, FileManifest] | None = None,
) -> dict[RevisionKey, Presentation]:
    """Resolve authorized exact revisions in bounded batches, without any writes.

    Explicit rows (including an intentionally empty entry) always win. Details
    can reuse their verified manifest; lists fetch names first, then only selected
    text bytes. Binary attachment blobs are not read to obtain directory labels.
    """
    keys = list(dict.fromkeys(revisions))
    result: dict[RevisionKey, Presentation] = {}
    if not keys:
        return result
    supplied = manifests or {}
    available = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='revision_presentations'"
    ).scalar_one_or_none()
    for start in range(0, len(keys), 200):
        batch = keys[start : start + 200]
        if available is not None:
            for saved in connection.execute(
                select(
                    RevisionPresentation.library_id,
                    RevisionPresentation.page_uid,
                    RevisionPresentation.revision_id,
                    RevisionPresentation.primary_file,
                    RevisionPresentation.display_title,
                ).where(
                    tuple_(
                        RevisionPresentation.library_id,
                        RevisionPresentation.page_uid,
                        RevisionPresentation.revision_id,
                    ).in_(batch)
                )
            ):
                result[(saved[0], saved[1], saved[2])] = Presentation(saved[3], saved[4])
        missing = [key for key in batch if key not in result]
        if not missing:
            continue
        titles = {
            (page_row[0], page_row[1], page_row[2]): page_row[3]
            for page_row in connection.execute(
                select(Page.library_id, Page.page_uid, Revision.revision_id, Page.title)
                .join(
                    Revision,
                    (Revision.library_id == Page.library_id) & (Revision.page_uid == Page.page_uid),
                )
                .where(tuple_(Page.library_id, Page.page_uid, Revision.revision_id).in_(missing))
            )
        }
        names: dict[RevisionKey, list[str]] = {key: [] for key in missing if key not in supplied}
        if names:
            for file_name in connection.execute(
                select(
                    RevisionFile.library_id,
                    RevisionFile.page_uid,
                    RevisionFile.revision_id,
                    RevisionFile.filename,
                ).where(
                    tuple_(
                        RevisionFile.library_id, RevisionFile.page_uid, RevisionFile.revision_id
                    ).in_(list(names))
                )
            ):
                names[(file_name[0], file_name[1], file_name[2])].append(file_name[3])
        selected = {
            (*key, name)
            for key, filenames in names.items()
            if (name := legacy_entry(filenames)) is not None
        }
        entries: dict[RevisionKey, ManifestFile | None] = {}
        if selected:
            for file in connection.execute(
                select(
                    RevisionFile.library_id,
                    RevisionFile.page_uid,
                    RevisionFile.revision_id,
                    RevisionFile.filename,
                    RevisionFile.content_bytes,
                    RevisionFile.size_bytes,
                    RevisionFile.content_sha256,
                ).where(
                    tuple_(
                        RevisionFile.library_id,
                        RevisionFile.page_uid,
                        RevisionFile.revision_id,
                        RevisionFile.filename,
                    ).in_(selected)
                )
            ):
                entries[(file[0], file[1], file[2])] = ManifestFile(
                    file[3], file[4], file[5], file[6]
                )
        for key in missing:
            if key in supplied:
                result[key] = legacy_presentation(supplied[key], titles.get(key, ""))
            else:
                result[key] = _derived(entries.get(key), titles.get(key, ""))
    return result
