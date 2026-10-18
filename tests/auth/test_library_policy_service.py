"""The application authorizer keeps legacy and per-credential Library modes separate."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, insert

from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    AuthorizationError,
)
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

AGENT_ID = "4" * 32
OTHER_AGENT_ID = "5" * 32
OPTED_CREDENTIAL_ID = "6" * 32
LEGACY_CREDENTIAL_ID = "7" * 32
OTHER_CREDENTIAL_ID = "8" * 32
CREATED_AT = 2_000_000
ACTIVE_AT = 3_000_000
EXPIRES_AT = 10_000_000


def _second_library(engine: Engine) -> tuple[str, str]:
    ids = iter(("a" * 32, "b" * 32, "c" * 32))
    with immediate_transaction(engine) as connection:
        seeded = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(ids),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Second Synthetic Authorization Library",
                section_name="Second Synthetic Section",
                book_name="Second Synthetic Book",
            )
        )
    return seeded.library.id, seeded.section.id


def _credentials(
    engine: Engine, home_library_id: str, home_section_id: str
) -> tuple[str, str, str]:
    values: list[str] = []
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        for caller_id in (AGENT_ID, OTHER_AGENT_ID):
            repository.add_caller(
                NewCaller(
                    id=caller_id,
                    library_id=home_library_id,
                    kind=CallerKind.AGENT,
                    name=f"Synthetic Agent {caller_id[0]}",
                    created_at=CREATED_AT,
                    updated_at=CREATED_AT,
                )
            )
        for credential_id, caller_id in (
            (OPTED_CREDENTIAL_ID, AGENT_ID),
            (LEGACY_CREDENTIAL_ID, AGENT_ID),
            (OTHER_CREDENTIAL_ID, OTHER_AGENT_ID),
        ):
            token = generate_token()
            repository.add_credential(
                NewCredential(
                    id=credential_id,
                    library_id=home_library_id,
                    caller_id=caller_id,
                    selector=token.selector,
                    token_version=token.version,
                    verifier=token.verifier,
                    expires_at=EXPIRES_AT,
                    created_at=CREATED_AT,
                    updated_at=CREATED_AT,
                )
            )
            values.append(token.value)
        repository.add_grant(
            NewSectionGrant(
                library_id=home_library_id,
                caller_id=AGENT_ID,
                section_id=home_section_id,
                action=SectionAction.PAGE_READ,
                created_at=CREATED_AT,
            )
        )
    return values[0], values[1], values[2]


def _opt_in(engine: Engine, home_library_id: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": OPTED_CREDENTIAL_ID,
                "caller_id": AGENT_ID,
                "home_library_id": home_library_id,
                "mode": "library_grants",
                "created_at": ACTIVE_AT,
            },
        )


def _grant(engine: Engine, home_library_id: str, target_library_id: str, action: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": OPTED_CREDENTIAL_ID,
                "caller_id": AGENT_ID,
                "home_library_id": home_library_id,
                "target_library_id": target_library_id,
                "action": action,
                "created_at": ACTIVE_AT,
            },
        )


def _authorize(
    engine: Engine,
    token: str,
    library_id: str,
    section_id: str,
    action: SectionAction,
    *,
    at: int = ACTIVE_AT,
) -> str:
    with immediate_transaction(engine) as connection:
        result = AuthenticationService(
            AuthRepository(connection), clock=lambda: at
        ).authorize_content(token, library_id=library_id, section_id=section_id, action=action)
        return result.credential.id


def test_legacy_mode_stays_home_library_and_section_scoped(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    target, target_section = _second_library(auth_engine)
    _, legacy, _ = _credentials(auth_engine, home, section)

    assert _authorize(auth_engine, legacy, home, section, SectionAction.PAGE_READ) == (
        LEGACY_CREDENTIAL_ID
    )
    with pytest.raises(AuthorizationError):
        _authorize(auth_engine, legacy, home, section, SectionAction.ARCHIVE_WRITE)
    with pytest.raises(AuthorizationError):
        _authorize(auth_engine, legacy, target, target_section, SectionAction.PAGE_READ)


def test_opted_in_policy_defaults_to_deny_and_ignores_old_home_section_grants(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    target, target_section = _second_library(auth_engine)
    opted, legacy, _ = _credentials(auth_engine, home, section)
    _opt_in(auth_engine, home)

    with pytest.raises(AuthorizationError):
        _authorize(auth_engine, opted, home, section, SectionAction.PAGE_READ)
    with pytest.raises(AuthorizationError):
        _authorize(auth_engine, opted, target, target_section, SectionAction.PAGE_READ)
    assert _authorize(auth_engine, legacy, home, section, SectionAction.PAGE_READ) == (
        LEGACY_CREDENTIAL_ID
    )


def test_library_read_and_write_are_independent_and_credential_specific(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    target, target_section = _second_library(auth_engine)
    opted, legacy, unrelated = _credentials(auth_engine, home, section)
    _opt_in(auth_engine, home)
    _grant(auth_engine, home, target, "write")

    assert _authorize(auth_engine, opted, target, target_section, SectionAction.ARCHIVE_WRITE) == (
        OPTED_CREDENTIAL_ID
    )
    for read_action in (SectionAction.QUERY, SectionAction.PAGE_READ):
        with pytest.raises(AuthorizationError):
            _authorize(auth_engine, opted, target, target_section, read_action)
    for token in (legacy, unrelated):
        with pytest.raises(AuthorizationError):
            _authorize(auth_engine, token, target, target_section, SectionAction.ARCHIVE_WRITE)

    _grant(auth_engine, home, target, "read")
    for read_action in (SectionAction.QUERY, SectionAction.PAGE_READ):
        assert _authorize(auth_engine, opted, target, target_section, read_action) == (
            OPTED_CREDENTIAL_ID
        )
    with pytest.raises(AuthorizationError):
        _authorize(auth_engine, opted, home, section, SectionAction.PAGE_READ)


def test_inactive_policy_identity_fails_closed_after_initial_authentication(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    target, target_section = _second_library(auth_engine)
    opted, _, _ = _credentials(auth_engine, home, section)
    _opt_in(auth_engine, home)
    _grant(auth_engine, home, target, "read")
    times = iter((ACTIVE_AT, EXPIRES_AT))

    with pytest.raises(AuthenticationError), immediate_transaction(auth_engine) as connection:
        AuthenticationService(
            AuthRepository(connection), clock=lambda: next(times)
        ).authorize_content(
            opted,
            library_id=target,
            section_id=target_section,
            action=SectionAction.PAGE_READ,
        )
