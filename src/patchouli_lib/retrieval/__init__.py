"""Authorization-aware, non-search retrieval application services."""

from patchouli_lib.retrieval.repository import RetrievalRepository
from patchouli_lib.retrieval.schemas import (
    BookView,
    CurrentPageRead,
    KeysetPage,
    PageDocument,
    PageMetadata,
    PageView,
    ReadWindow,
    RevisionFileManifestView,
    RevisionFileRead,
    RevisionFileView,
    RevisionView,
    SectionView,
)
from patchouli_lib.retrieval.service import (
    RetrievalAuthenticationError,
    RetrievalAuthorizationError,
    RetrievalNotFoundError,
    RetrievalPersistenceError,
    RetrievalService,
)

__all__ = [
    "BookView",
    "CurrentPageRead",
    "KeysetPage",
    "PageDocument",
    "PageMetadata",
    "PageView",
    "ReadWindow",
    "RevisionFileManifestView",
    "RevisionFileRead",
    "RevisionFileView",
    "RetrievalAuthenticationError",
    "RetrievalAuthorizationError",
    "RetrievalNotFoundError",
    "RetrievalPersistenceError",
    "RetrievalRepository",
    "RetrievalService",
    "RevisionView",
    "SectionView",
]
