from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


def _valid_utf8(value: str) -> str:
    value.encode("utf-8")
    return value


OpaqueId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
ResourceName = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_valid_utf8)]
BoundedText = Annotated[str, Field(max_length=4_000), AfterValidator(_valid_utf8)]
TimestampMicros = Annotated[int, Field(ge=0)]
ReadmeMarkdown = Annotated[
    str, StringConstraints(strip_whitespace=False), AfterValidator(_valid_utf8)
]


class LibrarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NewLibrary(LibrarySchema):
    id: OpaqueId
    name: ResourceName
    description: BoundedText = ""
    readme: ReadmeMarkdown = ""
    created_at: TimestampMicros
    updated_at: TimestampMicros


class LibraryRecord(NewLibrary):
    pass


class NewSection(LibrarySchema):
    id: OpaqueId
    library_id: OpaqueId
    name: ResourceName
    description: BoundedText = ""
    readme: ReadmeMarkdown = ""
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
    readme: ReadmeMarkdown = ""
    created_at: TimestampMicros
    updated_at: TimestampMicros


class BookRecord(NewBook):
    pass


class LibraryStructureSeed(LibrarySchema):
    library_name: ResourceName
    library_description: BoundedText = ""
    section_name: ResourceName
    section_description: BoundedText = ""
    book_name: ResourceName
    book_summary: BoundedText = ""


class CreateLibraryInput(LibrarySchema):
    name: ResourceName
    description: BoundedText = ""
    readme: ReadmeMarkdown = ""


class UpdateLibraryInput(LibrarySchema):
    name: ResourceName
    description: BoundedText
    expected_updated_at: TimestampMicros


class CreateSectionInput(LibrarySchema):
    name: ResourceName
    description: BoundedText = ""
    readme: ReadmeMarkdown = ""


class UpdateSectionInput(LibrarySchema):
    name: ResourceName
    description: BoundedText
    expected_updated_at: TimestampMicros


class CreateBookInput(LibrarySchema):
    name: ResourceName
    summary: BoundedText = ""
    readme: ReadmeMarkdown = ""


class UpdateBookInput(LibrarySchema):
    name: ResourceName
    summary: BoundedText
    expected_updated_at: TimestampMicros


class CreatedResources(LibrarySchema):
    library: bool
    section: bool
    book: bool


class SeededLibraryStructure(LibrarySchema):
    library: LibraryRecord
    section: SectionRecord
    book: BookRecord
    created: CreatedResources
