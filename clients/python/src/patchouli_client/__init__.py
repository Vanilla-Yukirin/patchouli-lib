from importlib.metadata import PackageNotFoundError, version

from patchouli_client.client import PatchouliClient
from patchouli_client.errors import (
    PatchouliClientError,
    ProblemError,
    ProtocolError,
    TransportError,
)
from patchouli_client.headers import CacheControl, ClientResponse, ResponseMetadata
from patchouli_client.models import (
    ApiLimits,
    ArchiveCreateMetadata,
    ArchiveRevisionMetadata,
    Book,
    Capabilities,
    Citation,
    CursorPage,
    Grant,
    IdempotencySupport,
    MarkdownContent,
    Page,
    PageDocument,
    ProblemDetails,
    Revision,
    SearchHit,
    SearchRequest,
    Section,
    SourceInput,
    WhoAmI,
)
from patchouli_client.secrets import BearerToken, IdempotencyKey
from patchouli_client.transport import OperationKind, RetryPolicy, Transport

try:
    __version__ = version("patchouli-client")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0+unknown"

__all__ = [
    "ApiLimits",
    "ArchiveCreateMetadata",
    "ArchiveRevisionMetadata",
    "BearerToken",
    "Book",
    "CacheControl",
    "Capabilities",
    "Citation",
    "ClientResponse",
    "CursorPage",
    "Grant",
    "IdempotencyKey",
    "IdempotencySupport",
    "MarkdownContent",
    "OperationKind",
    "Page",
    "PageDocument",
    "PatchouliClient",
    "PatchouliClientError",
    "ProblemDetails",
    "ProblemError",
    "ProtocolError",
    "ResponseMetadata",
    "RetryPolicy",
    "Revision",
    "SearchHit",
    "SearchRequest",
    "Section",
    "SourceInput",
    "Transport",
    "TransportError",
    "WhoAmI",
    "__version__",
]
