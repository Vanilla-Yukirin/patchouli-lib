from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, event, select

from patchouli_lib.api import auth_routes as auth_routes_module
from patchouli_lib.api.auth_contracts import (
    DEFAULT_CAPABILITY_CONFIGURATION,
    CapabilitiesResponse,
    CapabilityConfiguration,
    WhoAmIResponse,
)
from patchouli_lib.api.auth_routes import create_auth_router
from patchouli_lib.api.authentication import BearerAuthentication, extract_bearer_token
from patchouli_lib.api.contracts import PROTECTED_CACHE_CONTROL
from patchouli_lib.api.errors import (
    PROBLEM_MEDIA_TYPE,
    ApplicationProblem,
    install_api_exception_handlers,
)
from patchouli_lib.api.request_ids import REQUEST_ID_HEADER, RequestIDMiddleware
from patchouli_lib.auth.models import Caller, Credential
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    MAX_RFC3339_TIMESTAMP_MICROSECONDS,
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
    SectionGrantRecord,
)
from patchouli_lib.auth.service import CredentialExpiryError, CredentialIssuer
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed, NewSection
from patchouli_lib.library.service import LibrarySeedService

REQUEST_ID = f"req_{'9' * 32}"
PRIVATE_FAILURE = "synthetic-private-database-failure"


@dataclass(slots=True)
class MutableClock:
    value: int

    def __call__(self) -> int:
        return self.value


@dataclass(frozen=True, slots=True)
class AuthApiFixture:
    engine: Engine
    clock: MutableClock
    library_id: str
    first_section_id: str
    second_section_id: str
    agent_caller_id: str
    agent_credential_id: str
    agent_token: str
    operator_token: str
    expired_token: str
    revoked_token: str
    rotated_token: str


def _add_caller(
    repository: AuthRepository,
    *,
    caller_id: str,
    library_id: str,
    kind: CallerKind,
) -> None:
    repository.add_caller(
        NewCaller(
            id=caller_id,
            library_id=library_id,
            kind=kind,
            name=f"Synthetic {kind.value} {caller_id[0]}",
            description="Synthetic route fixture",
            created_at=1_000_000,
            updated_at=1_000_000,
        )
    )


def _issue_credential(
    repository: AuthRepository,
    *,
    library_id: str,
    caller_id: str,
    credential_id: str,
    expires_at: int,
) -> str:
    caller = repository.get_caller(library_id, caller_id)
    assert caller is not None
    return (
        CredentialIssuer(
            repository,
            id_factory=lambda: credential_id,
            clock=lambda: 1_000_000,
        )
        .issue(caller, expires_at=expires_at)
        .value
    )


@pytest.fixture
def auth_api(tmp_path: Path) -> Iterator[AuthApiFixture]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'auth-routes.db').as_posix()}")
    Caller.metadata.create_all(engine)
    clock = MutableClock(400_000_000)
    try:
        identifiers = iter(("1" * 32, "2" * 32, "3" * 32))
        with immediate_transaction(engine) as connection:
            structure = LibrarySeedService(
                LibraryRepository(connection),
                id_factory=lambda: next(identifiers),
                clock=lambda: 500_000,
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic HTTP Library",
                    section_name="Synthetic First Section",
                    book_name="Synthetic HTTP Book",
                )
            )
            second_section_id = "4" * 32
            LibraryRepository(connection).add_section(
                NewSection(
                    id=second_section_id,
                    library_id=structure.library.id,
                    name="Synthetic Second Section",
                    created_at=500_000,
                    updated_at=500_000,
                )
            )
            repository = AuthRepository(connection)
            caller_ids = {
                "agent": "a" * 32,
                "operator": "b" * 32,
                "expired": "c" * 32,
                "revoked": "d" * 32,
                "rotated": "e" * 32,
            }
            for name, caller_id in caller_ids.items():
                _add_caller(
                    repository,
                    caller_id=caller_id,
                    library_id=structure.library.id,
                    kind=CallerKind.OPERATOR if name == "operator" else CallerKind.AGENT,
                )

            agent_token = _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["agent"],
                credential_id="f" * 32,
                expires_at=1_000_000_000,
            )
            operator_token = _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["operator"],
                credential_id="0" * 32,
                expires_at=1_000_000_000,
            )
            expired_token = _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["expired"],
                credential_id="5" * 32,
                expires_at=100_000_000,
            )
            revoked_token = _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["revoked"],
                credential_id="6" * 32,
                expires_at=1_000_000_000,
            )
            rotated_token = _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["rotated"],
                credential_id="7" * 32,
                expires_at=1_000_000_000,
            )
            _issue_credential(
                repository,
                library_id=structure.library.id,
                caller_id=caller_ids["rotated"],
                credential_id="8" * 32,
                expires_at=1_000_000_000,
            )

            revoked = repository.get_credential(
                structure.library.id,
                caller_ids["revoked"],
                "6" * 32,
            )
            rotated = repository.get_credential(
                structure.library.id,
                caller_ids["rotated"],
                "7" * 32,
            )
            assert revoked is not None and rotated is not None
            repository.revoke_credential(revoked, revoked_at=200_000_000)
            repository.mark_credential_rotated(
                rotated,
                "8" * 32,
                rotated_at=200_000_000,
            )

            for caller_id, section_id, action in (
                (caller_ids["agent"], second_section_id, SectionAction.QUERY),
                (caller_ids["agent"], structure.section.id, SectionAction.QUERY),
                (caller_ids["agent"], structure.section.id, SectionAction.PAGE_READ),
                (caller_ids["agent"], second_section_id, SectionAction.ARCHIVE_WRITE),
                (caller_ids["operator"], structure.section.id, SectionAction.QUERY),
            ):
                repository.add_grant(
                    NewSectionGrant(
                        library_id=structure.library.id,
                        caller_id=caller_id,
                        section_id=section_id,
                        action=action,
                        created_at=2_000_000,
                    )
                )

        yield AuthApiFixture(
            engine=engine,
            clock=clock,
            library_id=structure.library.id,
            first_section_id=structure.section.id,
            second_section_id=second_section_id,
            agent_caller_id=caller_ids["agent"],
            agent_credential_id="f" * 32,
            agent_token=agent_token,
            operator_token=operator_token,
            expired_token=expired_token,
            revoked_token=revoked_token,
            rotated_token=rotated_token,
        )
    finally:
        engine.dispose()


def _build_app(
    fixture: AuthApiFixture,
    *,
    configuration: CapabilityConfiguration = DEFAULT_CAPABILITY_CONFIGURATION,
) -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_api_exception_handlers(application)
    application.add_middleware(
        RequestIDMiddleware,
        request_id_factory=lambda: REQUEST_ID,
    )
    application.include_router(
        create_auth_router(
            fixture.engine,
            capability_configuration=configuration,
            clock=fixture.clock,
        )
    )
    return application


def _authorization(token: str, *, scheme: str = "Bearer") -> dict[str, str]:
    return {"Authorization": f"{scheme} {token}"}


def _raw_request(headers: Sequence[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/capabilities",
            "headers": headers,
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )


def _credential_row(fixture: AuthApiFixture) -> dict[str, object]:
    with fixture.engine.connect() as connection:
        row = (
            connection.execute(
                select(Credential.__table__).where(Credential.id == fixture.agent_credential_id)
            )
            .mappings()
            .one()
        )
    return dict(row)


def _client_model(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> object:
    client_package = (
        Path(__file__).resolve().parents[2] / "clients" / "python" / "src" / "patchouli_client"
    )
    package = ModuleType("patchouli_client")
    package.__dict__["__path__"] = [str(client_package)]
    monkeypatch.setitem(sys.modules, "patchouli_client", package)

    loaded: ModuleType | None = None
    for module_name in ("errors", "models"):
        qualified_name = f"patchouli_client.{module_name}"
        spec = spec_from_file_location(qualified_name, client_package / f"{module_name}.py")
        assert spec is not None and spec.loader is not None
        loaded = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, qualified_name, loaded)
        spec.loader.exec_module(loaded)
    assert loaded is not None
    return getattr(loaded, name)


def test_router_inventory_and_default_capabilities_match_client_schema(
    auth_api: AuthApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = create_auth_router(auth_api.engine, clock=auth_api.clock)
    assert [(cast(Any, route).path, cast(Any, route).methods) for route in router.routes] == [
        ("/api/v1/capabilities", {"GET"}),
        ("/api/v1/auth/whoami", {"GET"}),
    ]
    application = _build_app(auth_api)

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={
                **_authorization(auth_api.agent_token, scheme="bEaReR"),
                "X-Request-ID": "req_untrusted",
            },
        )
        unknown = client.get(
            "/api/v1/auth/management",
            headers=_authorization(auth_api.agent_token),
        )

    assert response.status_code == 200
    assert unknown.status_code == 404
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json() == {
        "api_versions": ["v1"],
        "features": [],
        "limits": {
            "max_content_bytes": 2 * 1024 * 1024,
            "default_page_size": 20,
            "max_page_size": 100,
            "max_query_bytes": 4096,
        },
        "idempotency": {
            "content_mutations": False,
            "successful_replay_retention": "unsupported",
        },
    }
    client_model = cast(Any, _client_model(monkeypatch, "Capabilities"))
    parsed = client_model.from_dict(response.json())
    assert parsed.api_versions == ("v1",)
    assert parsed.features == ()
    assert parsed.idempotency.content_mutations is False


def test_integrator_capability_configuration_is_explicit_and_immutable(
    auth_api: AuthApiFixture,
) -> None:
    configuration = CapabilityConfiguration(
        features=("archive", "search"),
        content_mutation_idempotency=True,
        successful_replay_retention="indefinite-alpha",
    )
    with TestClient(_build_app(auth_api, configuration=configuration)) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers=_authorization(auth_api.agent_token),
        )

    parsed = CapabilitiesResponse.model_validate(response.json())
    assert parsed.features == ("archive", "search")
    assert parsed.idempotency.content_mutations is True
    assert parsed.idempotency.successful_replay_retention == "indefinite-alpha"
    with pytest.raises(ValidationError):
        configuration.__setattr__("features", ("search",))
    with pytest.raises(ValidationError):
        CapabilityConfiguration(features=("search", "archive"))


def test_agent_whoami_is_minimal_deterministic_and_client_parseable(
    auth_api: AuthApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(_build_app(auth_api)) as client:
        response = client.get(
            "/api/v1/auth/whoami",
            headers={"aUtHoRiZaTiOn": f"Bearer {auth_api.agent_token}"},
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    payload = response.json()
    assert set(payload) == {
        "caller_id",
        "credential_id",
        "kind",
        "expires_at",
        "policy_version",
        "grants",
    }
    assert payload == {
        "caller_id": auth_api.agent_caller_id,
        "credential_id": auth_api.agent_credential_id,
        "kind": "agent",
        "expires_at": "1970-01-01T00:16:40.000000Z",
        "policy_version": 1,
        "grants": [
            {
                "section_id": auth_api.first_section_id,
                "actions": ["page:read", "section:query"],
            },
            {
                "section_id": auth_api.second_section_id,
                "actions": ["archive:write", "section:query"],
            },
        ],
    }
    client_model = cast(Any, _client_model(monkeypatch, "WhoAmI"))
    parsed = client_model.from_dict(payload)
    assert parsed.caller_id == auth_api.agent_caller_id
    assert parsed.grants[0].actions == ("page:read", "section:query")
    forbidden = {
        "library_id",
        "name",
        "description",
        "selector",
        "verifier",
        "token_version",
        "last_used_at",
        "source",
    }
    assert all(term not in response.text.casefold() for term in forbidden)
    assert auth_api.agent_token not in response.text
    assert auth_api.agent_token.split(".")[1] not in response.text


def test_rfc3339_maximum_expiry_is_issued_and_serialized_by_whoami(
    auth_api: AuthApiFixture,
) -> None:
    credential_id = "9" * 32
    with immediate_transaction(auth_api.engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(auth_api.library_id, auth_api.agent_caller_id)
        assert caller is not None
        issued = CredentialIssuer(
            repository,
            id_factory=lambda: credential_id,
            clock=lambda: 1_000_000,
        ).issue(
            caller,
            expires_at=MAX_RFC3339_TIMESTAMP_MICROSECONDS,
        )
        assert issued.credential.expires_at == MAX_RFC3339_TIMESTAMP_MICROSECONDS

    with TestClient(_build_app(auth_api)) as client:
        response = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(issued.value),
        )

    assert response.status_code == 200
    assert response.json()["credential_id"] == credential_id
    assert response.json()["expires_at"] == "9999-12-31T23:59:59.999999Z"


def test_expiry_above_rfc3339_maximum_is_rejected_before_storage(
    auth_api: AuthApiFixture,
) -> None:
    over_maximum = MAX_RFC3339_TIMESTAMP_MICROSECONDS + 1
    synthetic = generate_token()
    with immediate_transaction(auth_api.engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(auth_api.library_id, auth_api.agent_caller_id)
        assert caller is not None
        before = len(
            repository.list_active_credentials(
                auth_api.library_id,
                auth_api.agent_caller_id,
                active_at=2_000_000,
            )
        )

        with pytest.raises(CredentialExpiryError):
            CredentialIssuer(
                repository,
                id_factory=lambda: "9" * 32,
                clock=lambda: 1_000_000,
            ).issue(caller, expires_at=over_maximum)
        with pytest.raises(ValidationError):
            NewCredential(
                id="9" * 32,
                library_id=auth_api.library_id,
                caller_id=auth_api.agent_caller_id,
                selector=synthetic.selector,
                token_version=synthetic.version,
                verifier=synthetic.verifier,
                expires_at=over_maximum,
                created_at=1_000_000,
                updated_at=1_000_000,
            )

        after = len(
            repository.list_active_credentials(
                auth_api.library_id,
                auth_api.agent_caller_id,
                active_at=2_000_000,
            )
        )

    assert after == before


def test_operator_may_use_diagnostics_but_receives_no_content_grants(
    auth_api: AuthApiFixture,
) -> None:
    with TestClient(_build_app(auth_api)) as client:
        capabilities = client.get(
            "/api/v1/capabilities",
            headers=_authorization(auth_api.operator_token),
        )
        whoami = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(auth_api.operator_token),
        )

    assert capabilities.status_code == 200
    parsed = WhoAmIResponse.model_validate(whoami.json())
    assert parsed.kind is CallerKind.OPERATOR
    assert parsed.grants == ()


def test_missing_credential_uses_authentication_required_problem(
    auth_api: AuthApiFixture,
) -> None:
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 401
    assert response.headers["Content-Type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == PROTECTED_CACHE_CONTROL
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert response.json()["code"] == "authentication_required"


@pytest.mark.parametrize(
    ("header_name", "scheme"),
    [
        (b"authorization", b"Bearer"),
        (b"Authorization", b"Bearer"),
        (b"aUtHoRiZaTiOn", b"bEaReR"),
    ],
)
def test_bearer_bridge_returns_exact_token_only_for_one_valid_header(
    auth_api: AuthApiFixture,
    header_name: bytes,
    scheme: bytes,
) -> None:
    token = auth_api.agent_token.encode("ascii")
    request = _raw_request([(header_name, scheme + b" " + token)])

    extracted = extract_bearer_token(request)

    assert extracted == auth_api.agent_token
    assert type(extracted) is str
    assert "state" not in request.scope


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ([], "authentication_required"),
        ([(b"authorization", b"")], "invalid_token"),
        ([(b"authorization", b"Bearer \xff")], "invalid_token"),
        ([(b"authorization", b"Bearer " + b"x" * 250)], "invalid_token"),
        ([(b"authorization", b"Bearer  synthetic")], "invalid_token"),
        ([(b"authorization", b"Basic synthetic")], "invalid_token"),
        ([(b"authorization", b"Bearer")], "invalid_token"),
        (
            [
                (b"authorization", b"Bearer synthetic"),
                (b"Authorization", b"Bearer synthetic"),
            ],
            "invalid_token",
        ),
    ],
    ids=(
        "missing",
        "empty",
        "non-ascii",
        "over-256",
        "multiple-spaces",
        "unsupported-scheme",
        "missing-token",
        "duplicate-mixed-case-name",
    ),
)
def test_bearer_bridge_rejects_invalid_raw_headers_with_fixed_safe_errors(
    headers: list[tuple[bytes, bytes]],
    expected_code: str,
) -> None:
    with pytest.raises(ApplicationProblem) as exc_info:
        extract_bearer_token(_raw_request(headers))

    assert exc_info.value.code == expected_code


def test_bridge_and_bearer_authentication_share_all_parsing_outcomes(
    auth_api: AuthApiFixture,
) -> None:
    valid = f"Bearer {auth_api.agent_token}".encode("ascii")
    cases: tuple[tuple[tuple[tuple[bytes, bytes], ...], str | None], ...] = (
        (((b"authorization", valid),), None),
        (((b"AuThOrIzAtIoN", b"bEaReR " + auth_api.agent_token.encode("ascii")),), None),
        ((), "authentication_required"),
        (((b"authorization", b""),), "invalid_token"),
        (((b"authorization", b"Bearer \xff"),), "invalid_token"),
        (((b"authorization", b"Bearer " + b"x" * 250),), "invalid_token"),
        (((b"authorization", b"Bearer  synthetic"),), "invalid_token"),
        (((b"authorization", b"Basic synthetic"),), "invalid_token"),
        (
            (
                (b"authorization", valid),
                (b"AUTHORIZATION", valid),
            ),
            "invalid_token",
        ),
    )
    authenticate = BearerAuthentication(auth_api.engine, clock=auth_api.clock)

    for headers, expected_code in cases:
        if expected_code is None:
            assert extract_bearer_token(_raw_request(headers)) == auth_api.agent_token
            context = authenticate(_raw_request(headers))
            assert context.authenticated.caller.id == auth_api.agent_caller_id
            continue

        with pytest.raises(ApplicationProblem) as bridge_error:
            extract_bearer_token(_raw_request(headers))
        with pytest.raises(ApplicationProblem) as authentication_error:
            authenticate(_raw_request(headers))
        assert bridge_error.value.code == authentication_error.value.code == expected_code


def test_bearer_bridge_function_and_errors_never_render_candidate_token() -> None:
    candidate = "synthetic-private-candidate"
    request = _raw_request([(b"authorization", f"Bearer  {candidate}".encode("ascii"))])

    with pytest.raises(ApplicationProblem) as exc_info:
        extract_bearer_token(request)

    rendered = f"{extract_bearer_token!r} {exc_info.value!s} {exc_info.value!r}"
    assert candidate not in rendered
    assert "authorization" not in vars(exc_info.value)


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic synthetic",
        "Bearer",
        "Bearer ",
        "Bearer  synthetic",
        " Bearer synthetic",
        "Bearer\tsynthetic",
        "Bearer synthetic ",
        f"Bearer {'x' * 300}",
    ],
)
def test_malformed_unsupported_whitespace_and_overlength_credentials_are_invalid(
    auth_api: AuthApiFixture,
    authorization: str,
) -> None:
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"Authorization": authorization},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert authorization not in response.text


def test_duplicate_mixed_case_authorization_headers_are_invalid(
    auth_api: AuthApiFixture,
) -> None:
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers=[
                ("Authorization", f"Bearer {auth_api.agent_token}"),
                ("aUtHoRiZaTiOn", f"Bearer {auth_api.agent_token}"),
            ],
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert auth_api.agent_token not in response.text


@pytest.mark.parametrize("token_name", ["expired_token", "revoked_token", "rotated_token"])
def test_expired_revoked_and_rotated_credentials_are_indistinguishable(
    auth_api: AuthApiFixture,
    token_name: str,
) -> None:
    token = getattr(auth_api, token_name)
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(token),
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert token not in response.text
    assert token.split(".")[1] not in response.text


def test_unknown_and_disabled_credentials_fail_before_disclosure(
    auth_api: AuthApiFixture,
) -> None:
    unknown_token = generate_token().value
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        unknown = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(unknown_token),
        )
    with immediate_transaction(auth_api.engine) as connection:
        disabled = AuthRepository(connection).disable_caller(
            auth_api.library_id,
            auth_api.agent_caller_id,
            disabled_at=450_000_000,
        )
        assert disabled is not None
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        disabled_response = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(auth_api.agent_token),
        )

    for response in (unknown, disabled_response):
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_token"
        assert "library_id" not in response.text
        assert "grants" not in response.text


def test_policy_and_grant_changes_are_visible_on_the_next_request(
    auth_api: AuthApiFixture,
) -> None:
    with TestClient(_build_app(auth_api)) as client:
        before = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(auth_api.agent_token),
        )
    with immediate_transaction(auth_api.engine) as connection:
        repository = AuthRepository(connection)
        assert repository.remove_grant(
            auth_api.library_id,
            auth_api.agent_caller_id,
            auth_api.first_section_id,
            SectionAction.PAGE_READ,
        )
        updated = repository.increment_policy_version(
            auth_api.library_id,
            auth_api.agent_caller_id,
            expected_version=1,
            updated_at=450_000_000,
        )
        assert updated is not None
    with TestClient(_build_app(auth_api)) as client:
        after = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(auth_api.agent_token),
        )

    assert before.json()["policy_version"] == 1
    assert before.json()["grants"][0]["actions"] == ["page:read", "section:query"]
    assert after.json()["policy_version"] == 2
    assert after.json()["grants"][0]["actions"] == ["section:query"]


def test_last_used_commits_coalesces_and_connection_closes_before_response_build(
    auth_api: AuthApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_connections = 0

    def checked_out(_dbapi: object, _record: object, _proxy: object) -> None:
        nonlocal active_connections
        active_connections += 1

    def checked_in(_dbapi: object, _record: object) -> None:
        nonlocal active_connections
        active_connections -= 1

    event.listen(auth_api.engine, "checkout", checked_out)
    event.listen(auth_api.engine, "checkin", checked_in)
    original = cast(
        Any,
        auth_routes_module.__dict__["capabilities_response"],
    )

    def observed_response(configuration: CapabilityConfiguration) -> CapabilitiesResponse:
        assert active_connections == 0
        return cast(CapabilitiesResponse, original(configuration))

    monkeypatch.setitem(
        auth_routes_module.__dict__,
        "capabilities_response",
        observed_response,
    )
    assert _credential_row(auth_api)["last_used_at"] is None

    with TestClient(_build_app(auth_api)) as client:
        first = client.get(
            "/api/v1/capabilities",
            headers=_authorization(auth_api.agent_token),
        )
        assert first.status_code == 200
        assert _credential_row(auth_api)["last_used_at"] == 400_000_000

        auth_api.clock.value = 500_000_000
        second = client.get(
            "/api/v1/capabilities",
            headers=_authorization(auth_api.agent_token),
        )
        assert second.status_code == 200
        assert _credential_row(auth_api)["last_used_at"] == 400_000_000

        auth_api.clock.value = 701_000_000
        third = client.get(
            "/api/v1/capabilities",
            headers=_authorization(auth_api.agent_token),
        )
        assert third.status_code == 200

    assert _credential_row(auth_api)["last_used_at"] == 701_000_000
    assert active_connections == 0


def test_runtime_failure_is_safe_and_rolls_back_last_used(
    auth_api: AuthApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_grant_read(
        _repository: AuthRepository,
        _library_id: str,
        _caller_id: str,
    ) -> tuple[SectionGrantRecord, ...]:
        raise RuntimeError(PRIVATE_FAILURE)

    monkeypatch.setattr(AuthRepository, "list_grants", fail_grant_read)
    assert _credential_row(auth_api)["last_used_at"] is None
    with TestClient(_build_app(auth_api), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/auth/whoami",
            headers=_authorization(auth_api.agent_token),
        )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert PRIVATE_FAILURE not in response.text
    assert auth_api.agent_token not in response.text
    assert auth_api.agent_token.split(".")[1] not in response.text
    assert _credential_row(auth_api)["last_used_at"] is None


def test_cancellation_like_base_exception_rolls_back_and_propagates(
    auth_api: AuthApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticCancellation(BaseException):
        pass

    def cancel_grant_read(
        _repository: AuthRepository,
        _library_id: str,
        _caller_id: str,
    ) -> tuple[SectionGrantRecord, ...]:
        raise SyntheticCancellation

    monkeypatch.setattr(AuthRepository, "list_grants", cancel_grant_read)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/whoami",
            "headers": [(b"authorization", f"Bearer {auth_api.agent_token}".encode("ascii"))],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    with pytest.raises(SyntheticCancellation):
        BearerAuthentication(auth_api.engine, clock=auth_api.clock)(request)

    assert _credential_row(auth_api)["last_used_at"] is None


def test_context_repr_is_token_free(auth_api: AuthApiFixture) -> None:
    auth_api.clock.value = 2_000_000
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/whoami",
            "headers": [(b"authorization", f"Bearer {auth_api.agent_token}".encode("ascii"))],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "http_version": "1.1",
        }
    )
    context = BearerAuthentication(auth_api.engine, clock=auth_api.clock)(request)
    rendered = repr(context)

    assert context.authenticated.caller.id == auth_api.agent_caller_id
    assert auth_api.agent_token not in rendered
    assert auth_api.agent_token.split(".")[1] not in rendered
    assert "selector" not in rendered.casefold()
    assert "verifier" not in rendered.casefold()
