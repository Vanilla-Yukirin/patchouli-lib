from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.library_policy import (
    LegacySectionPolicy,
    LibraryAction,
    LibraryGrantPolicy,
    resolve_library_policy,
)
from patchouli_lib.auth.models import CredentialLibraryGrant, CredentialLibraryPolicy
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import (
    CallerKind,
    NewCaller,
    NewCredential,
    NewSectionGrant,
    SectionAction,
)
from patchouli_lib.auth.service import AuthenticationService
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import LibraryStructureSeed
from patchouli_lib.library.service import LibrarySeedService

CALLER = "4" * 32
OTHER_CALLER = "5" * 32
CREDENTIAL = "6" * 32
OTHER_CREDENTIAL = "7" * 32
OPERATOR = "8" * 32
OPERATOR_CREDENTIAL = "9" * 32


def _seed_second_library(engine: Engine) -> str:
    identifiers = iter(("a" * 32, "b" * 32, "c" * 32))
    with immediate_transaction(engine) as connection:
        seeded = LibrarySeedService(
            LibraryRepository(connection),
            id_factory=lambda: next(identifiers),
            clock=lambda: 1_000_000,
        ).seed(
            LibraryStructureSeed(
                library_name="Second Synthetic Policy Library",
                section_name="Second Synthetic Section",
                book_name="Second Synthetic Book",
            )
        )
    return seeded.library.id


def _seed_credentials(engine: Engine, home_library_id: str, section_id: str) -> str:
    primary_token = ""
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        for caller_id, kind in (
            (CALLER, CallerKind.AGENT),
            (OTHER_CALLER, CallerKind.AGENT),
            (OPERATOR, CallerKind.OPERATOR),
        ):
            repository.add_caller(
                NewCaller(
                    id=caller_id,
                    library_id=home_library_id,
                    kind=kind,
                    name=f"Synthetic Policy {caller_id[0]}",
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
        for credential_id, caller_id in (
            (CREDENTIAL, CALLER),
            (OTHER_CREDENTIAL, OTHER_CALLER),
            (OPERATOR_CREDENTIAL, OPERATOR),
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
                    expires_at=10_000_000,
                    created_at=2_000_000,
                    updated_at=2_000_000,
                )
            )
            if credential_id == CREDENTIAL:
                primary_token = token.value
        repository.add_grant(
            NewSectionGrant(
                library_id=home_library_id,
                caller_id=CALLER,
                section_id=section_id,
                action=SectionAction.PAGE_READ,
                created_at=2_000_000,
            )
        )
    return primary_token


def _resolve(
    engine: Engine,
    home_library_id: str,
    target_library_id: str,
    *,
    credential_id: str = CREDENTIAL,
    caller_id: str = CALLER,
    active_at: int = 3_000_000,
) -> LegacySectionPolicy | LibraryGrantPolicy | None:
    with engine.connect() as connection:
        return resolve_library_policy(
            connection,
            credential_id=credential_id,
            caller_id=caller_id,
            home_library_id=home_library_id,
            target_library_id=target_library_id,
            active_at=active_at,
        )


def _opt_in(engine: Engine, home_library_id: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": home_library_id,
                "mode": "library_grants",
                "created_at": 3_000_000,
            },
        )


def _grant(engine: Engine, home_library_id: str, target_library_id: str, action: str) -> None:
    with immediate_transaction(engine) as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": home_library_id,
                "target_library_id": target_library_id,
                "action": action,
                "created_at": 3_000_000,
            },
        )


def test_legacy_credential_retains_section_behavior_until_opt_in(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    other = _seed_second_library(auth_engine)
    token = _seed_credentials(auth_engine, home, section)

    assert isinstance(_resolve(auth_engine, home, other), LegacySectionPolicy)
    with immediate_transaction(auth_engine) as connection:
        authenticated = AuthenticationService(
            AuthRepository(connection), clock=lambda: 3_000_000
        ).authorize_content(
            token, library_id=home, section_id=section, action=SectionAction.PAGE_READ
        )
        assert authenticated.caller.id == CALLER
    assert _resolve(auth_engine, home, other, caller_id=OTHER_CALLER) is None
    assert _resolve(auth_engine, home, other, credential_id=OTHER_CREDENTIAL) is None
    assert (
        _resolve(auth_engine, home, other, credential_id=OPERATOR_CREDENTIAL, caller_id=OPERATOR)
        is None
    )


def test_new_mode_default_denies_and_read_write_are_independent(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    other = _seed_second_library(auth_engine)
    _seed_credentials(auth_engine, home, section)
    _opt_in(auth_engine, home)

    assert _resolve(auth_engine, home, other) == LibraryGrantPolicy(read=False, write=False)
    _grant(auth_engine, home, other, "write")
    write_only = _resolve(auth_engine, home, other)
    assert write_only == LibraryGrantPolicy(read=False, write=True)
    assert isinstance(write_only, LibraryGrantPolicy)
    assert write_only.allows(LibraryAction.WRITE)
    assert not write_only.allows(LibraryAction.READ)
    assert _resolve(auth_engine, home, home) == LibraryGrantPolicy(read=False, write=False)

    _grant(auth_engine, home, other, "read")
    assert _resolve(auth_engine, home, other) == LibraryGrantPolicy(read=True, write=True)
    assert isinstance(
        _resolve(auth_engine, home, other, credential_id=OTHER_CREDENTIAL, caller_id=OTHER_CALLER),
        LegacySectionPolicy,
    )
    assert _resolve(auth_engine, home, other, active_at=10_000_000) is None


def test_composite_foreign_keys_prevent_cross_identity_and_target_confusion(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, section, _ = scoped_library
    other = _seed_second_library(auth_engine)
    _seed_credentials(auth_engine, home, section)

    with pytest.raises(IntegrityError), immediate_transaction(auth_engine) as connection:
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": CREDENTIAL,
                "caller_id": OTHER_CALLER,
                "home_library_id": home,
                "mode": "library_grants",
                "created_at": 3_000_000,
            },
        )
    _opt_in(auth_engine, home)
    for caller_id, target_library_id in (
        (OTHER_CALLER, other),
        (CALLER, "f" * 32),
    ):
        with pytest.raises(IntegrityError), immediate_transaction(auth_engine) as connection:
            connection.execute(
                insert(CredentialLibraryGrant),
                {
                    "credential_id": CREDENTIAL,
                    "caller_id": caller_id,
                    "home_library_id": home,
                    "target_library_id": target_library_id,
                    "action": "read",
                    "created_at": 3_000_000,
                },
            )
    assert _resolve(auth_engine, home, other) == LibraryGrantPolicy(read=False, write=False)


def test_revocation_fails_closed(auth_engine: Engine, scoped_library: tuple[str, str, str]) -> None:
    home, section, _ = scoped_library
    other = _seed_second_library(auth_engine)
    _seed_credentials(auth_engine, home, section)
    _opt_in(auth_engine, home)
    _grant(auth_engine, home, other, "read")
    with immediate_transaction(auth_engine) as connection:
        credential = AuthRepository(connection).get_credential(home, CALLER, CREDENTIAL)
        assert credential is not None
        AuthRepository(connection).revoke_credential(credential, revoked_at=4_000_000)
    assert _resolve(auth_engine, home, other, active_at=4_000_000) is None


def test_policy_rejects_invalid_evaluation_inputs(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    home, _, _ = scoped_library
    with pytest.raises(ValueError, match="nonnegative"):
        _resolve(auth_engine, home, home, active_at=-1)
    with pytest.raises(ValueError, match="Unknown Library action"):
        LibraryGrantPolicy(read=True, write=True).allows(cast(LibraryAction, "delete"))
