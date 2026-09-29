from collections.abc import Callable
from time import time_ns
from uuid import uuid4

from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import (
    BookRecord,
    CreateBookInput,
    CreatedResources,
    CreateLibraryInput,
    CreateSectionInput,
    LibraryRecord,
    LibraryStructureSeed,
    NewBook,
    NewLibrary,
    NewSection,
    SectionRecord,
    SeededLibraryStructure,
    UpdateBookInput,
)

IdFactory = Callable[[], str]
Clock = Callable[[], int]


class LibrarySeedConflictError(RuntimeError):
    pass


class LibraryStructureNotFoundError(RuntimeError):
    pass


class LibraryStructureVersionConflictError(RuntimeError):
    pass


def _new_opaque_id() -> str:
    return uuid4().hex


def _utc_microseconds() -> int:
    return time_ns() // 1_000


class LibrarySeedService:
    """Seed local operator structure inside a caller-owned unit of work."""

    def __init__(
        self,
        repository: LibraryRepository,
        *,
        id_factory: IdFactory = _new_opaque_id,
        clock: Clock = _utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def seed(self, seed: LibraryStructureSeed) -> SeededLibraryStructure:
        timestamp: int | None = None

        def creation_time() -> int:
            nonlocal timestamp
            if timestamp is None:
                timestamp = self._clock()
            return timestamp

        library, library_created = self._seed_library(seed, creation_time)
        section, section_created = self._seed_section(seed, library, creation_time)
        book, book_created = self._seed_book(seed, library, section, creation_time)

        return SeededLibraryStructure(
            library=library,
            section=section,
            book=book,
            created=CreatedResources(
                library=library_created,
                section=section_created,
                book=book_created,
            ),
        )

    def _seed_library(
        self,
        seed: LibraryStructureSeed,
        creation_time: Clock,
    ) -> tuple[LibraryRecord, bool]:
        existing = self._repository.find_library_by_name(seed.library_name)
        if existing is not None:
            if existing.description != seed.library_description:
                raise LibrarySeedConflictError(
                    "Existing Library does not match requested seed metadata."
                )
            return existing, False

        timestamp = creation_time()
        created = self._repository.add_library(
            NewLibrary(
                id=self._id_factory(),
                name=seed.library_name,
                description=seed.library_description,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        return created, True

    def _seed_section(
        self,
        seed: LibraryStructureSeed,
        library: LibraryRecord,
        creation_time: Clock,
    ) -> tuple[SectionRecord, bool]:
        existing = self._repository.find_section_by_name(library.id, seed.section_name)
        if existing is not None:
            if existing.description != seed.section_description:
                raise LibrarySeedConflictError(
                    "Existing Section does not match requested seed metadata."
                )
            return existing, False

        timestamp = creation_time()
        created = self._repository.add_section(
            NewSection(
                id=self._id_factory(),
                library_id=library.id,
                name=seed.section_name,
                description=seed.section_description,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        return created, True

    def _seed_book(
        self,
        seed: LibraryStructureSeed,
        library: LibraryRecord,
        section: SectionRecord,
        creation_time: Clock,
    ) -> tuple[BookRecord, bool]:
        existing = self._repository.find_book_by_name(
            library.id,
            section.id,
            seed.book_name,
        )
        if existing is not None:
            if existing.summary != seed.book_summary:
                raise LibrarySeedConflictError(
                    "Existing Book does not match requested seed metadata."
                )
            return existing, False

        timestamp = creation_time()
        created = self._repository.add_book(
            NewBook(
                id=self._id_factory(),
                library_id=library.id,
                section_id=section.id,
                name=seed.book_name,
                summary=seed.book_summary,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        return created, True


class LibraryStructureService:
    """Create one explicit hierarchy item in a caller-owned transaction."""

    def __init__(
        self,
        repository: LibraryRepository,
        *,
        id_factory: IdFactory = _new_opaque_id,
        clock: Clock = _utc_microseconds,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    def create_library(self, request: CreateLibraryInput) -> LibraryRecord:
        if self._repository.find_library_by_name(request.name) is not None:
            raise LibrarySeedConflictError("Library name already exists.")
        now = self._clock()
        return self._repository.add_library(
            NewLibrary(
                id=self._id_factory(),
                name=request.name,
                description=request.description,
                created_at=now,
                updated_at=now,
            )
        )

    def create_section(self, library_id: str, request: CreateSectionInput) -> SectionRecord:
        if self._repository.get_library(library_id) is None:
            raise LibraryStructureNotFoundError("Library was not found.")
        if self._repository.find_section_by_name(library_id, request.name) is not None:
            raise LibrarySeedConflictError("Section name already exists in this Library.")
        now = self._clock()
        return self._repository.add_section(
            NewSection(
                id=self._id_factory(),
                library_id=library_id,
                name=request.name,
                description=request.description,
                created_at=now,
                updated_at=now,
            )
        )

    def create_book(self, library_id: str, section_id: str, request: CreateBookInput) -> BookRecord:
        if self._repository.get_section(library_id, section_id) is None:
            raise LibraryStructureNotFoundError("Section was not found in this Library.")
        if self._repository.find_book_by_name(library_id, section_id, request.name) is not None:
            raise LibrarySeedConflictError("Book name already exists in this Section.")
        now = self._clock()
        return self._repository.add_book(
            NewBook(
                id=self._id_factory(),
                library_id=library_id,
                section_id=section_id,
                name=request.name,
                summary=request.summary,
                created_at=now,
                updated_at=now,
            )
        )

    def update_book(
        self, library_id: str, section_id: str, book_id: str, request: UpdateBookInput
    ) -> tuple[BookRecord, bool]:
        current = self._repository.get_book(library_id, section_id, book_id)
        if current is None:
            raise LibraryStructureNotFoundError("Book was not found in this Section.")
        if current.updated_at != request.expected_updated_at:
            raise LibraryStructureVersionConflictError("Book changed since the form was opened.")
        if current.name == request.name and current.summary == request.summary:
            return current, False
        same_name = self._repository.find_book_by_name(library_id, section_id, request.name)
        if same_name is not None and same_name.id != book_id:
            raise LibrarySeedConflictError("Book name already exists in this Section.")
        updated = current.model_copy(
            update={
                "name": request.name,
                "summary": request.summary,
                "updated_at": max(self._clock(), current.updated_at + 1),
            }
        )
        if not self._repository.update_book_metadata(
            updated, expected_updated_at=request.expected_updated_at
        ):
            raise LibraryStructureVersionConflictError("Book changed since the form was opened.")
        return updated, True
