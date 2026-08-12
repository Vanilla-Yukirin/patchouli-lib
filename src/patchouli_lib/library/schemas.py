from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

OpaqueId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
ResourceName = Annotated[str, Field(min_length=1, max_length=200)]
BoundedText = Annotated[str, Field(max_length=4_000)]
TimestampMicros = Annotated[int, Field(ge=0)]


class LibrarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NewLibrary(LibrarySchema):
    id: OpaqueId
    name: ResourceName
    created_at: TimestampMicros
    updated_at: TimestampMicros


class LibraryRecord(NewLibrary):
    pass


class NewSection(LibrarySchema):
    id: OpaqueId
    library_id: OpaqueId
    name: ResourceName
    description: BoundedText = ""
    created_at: TimestampMicros
    updated_at: TimestampMicros


class SectionRecord(NewSection):
    pass


class NewBook(LibrarySchema):
    id: OpaqueId
    library_id: OpaqueId
    section_id: OpaqueId
    name: ResourceName
    summary: BoundedText = ""
    created_at: TimestampMicros
    updated_at: TimestampMicros


class BookRecord(NewBook):
    pass


class LibraryStructureSeed(LibrarySchema):
    library_name: ResourceName
    section_name: ResourceName
    section_description: BoundedText = ""
    book_name: ResourceName
    book_summary: BoundedText = ""


class CreatedResources(LibrarySchema):
    library: bool
    section: bool
    book: bool


class SeededLibraryStructure(LibrarySchema):
    library: LibraryRecord
    section: SectionRecord
    book: BookRecord
    created: CreatedResources
