from __future__ import annotations

import pytest
from sqlalchemy import Engine, insert, update

from patchouli_lib.api.auth_contracts import whoami_response
from patchouli_lib.api.authentication import AuthenticatedRequestContext
from patchouli_lib.auth.library_policy import LibraryGrantPolicy
from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, CallerRecord, NewCaller
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    CredentialExpiryError,
    CredentialIssuer,
)
from patchouli_lib.database import immediate_transaction

_CREATED = 2_000_000
_LATER = 4_000_000_000_000_000


def _caller(repository: AuthRepository, library_id: str, kind: CallerKind) -> CallerRecord:
    return repository.add_caller(
        NewCaller(
            id="4" * 32,
            library_id=library_id,
            kind=kind,
            name="Synthetic non-expiring identity",
            created_at=_CREATED,
            updated_at=_CREATED,
        )
    )


def test_non_expiring_agent_authentication_reveal_policy_and_whoami(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    library_id = scoped_library[0]
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        caller = _caller(repository, library_id, CallerKind.AGENT)
        issued = CredentialIssuer(repository, clock=lambda: _CREATED).issue(caller, expires_at=None)
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": issued.credential.id,
                "caller_id": caller.id,
                "home_library_id": library_id,
                "mode": "library_grants",
                "created_at": _CREATED,
            },
        )
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": issued.credential.id,
                "caller_id": caller.id,
                "home_library_id": library_id,
                "target_library_id": library_id,
                "action": "read",
                "created_at": _CREATED,
            },
        )
        authenticated = AuthenticationService(repository, clock=lambda: _LATER).authenticate(
            issued.value
        )
        assert authenticated.credential.expires_at is None
        assert authenticated.credential.last_used_at == _LATER
        assert len(repository.list_active_credentials(library_id, caller.id, active_at=_LATER)) == 1
        assert (
            repository.get_active_agent_token_value(
                library_id, caller.id, issued.credential.id, active_at=_LATER
            )
            == issued.value
        )
        assert repository.get_library_policy(
            credential_id=issued.credential.id,
            caller_id=caller.id,
            home_library_id=library_id,
            target_library_id=library_id,
            active_at=_LATER,
        ) == LibraryGrantPolicy(read=True, write=False)
        response = whoami_response(AuthenticatedRequestContext(authenticated, ()))
        assert response.model_dump(mode="json")["expires_at"] is None


@pytest.mark.parametrize("inactive", ["revoked", "disabled", "rotated"])
def test_non_expiring_agents_still_obey_lifecycle(
    auth_engine: Engine, scoped_library: tuple[str, str, str], inactive: str
) -> None:
    library_id = scoped_library[0]
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        caller = _caller(repository, library_id, CallerKind.AGENT)
        issued = CredentialIssuer(repository, clock=lambda: _CREATED).issue(caller, expires_at=None)
        credential = repository.get_credential(library_id, caller.id, issued.credential.id)
        assert credential is not None
        if inactive == "revoked":
            repository.revoke_credential(credential, revoked_at=_CREATED + 1)
        elif inactive == "rotated":
            replacement = CredentialIssuer(repository, clock=lambda: _CREATED + 1).issue(
                caller, expires_at=_LATER + 1
            )
            repository.mark_credential_rotated(
                credential, replacement.credential.id, rotated_at=_CREATED + 1
            )
            assert (
                AuthenticationService(repository, clock=lambda: _LATER)
                .authenticate(replacement.value)
                .credential.expires_at
                == _LATER + 1
            )
        else:
            connection.execute(
                update(Caller)
                .where(Caller.id == caller.id)
                .values(disabled_at=_CREATED + 1, updated_at=_CREATED + 1)
            )
        with pytest.raises(AuthenticationError):
            AuthenticationService(repository, clock=lambda: _LATER).authenticate(issued.value)
        assert (
            repository.get_active_agent_token_value(
                library_id, caller.id, issued.credential.id, active_at=_LATER
            )
            is None
        )
        assert (
            repository.get_library_policy(
                credential_id=issued.credential.id,
                caller_id=caller.id,
                home_library_id=library_id,
                target_library_id=library_id,
                active_at=_LATER,
            )
            is None
        )


def test_operator_still_requires_finite_expiry(
    auth_engine: Engine, scoped_library: tuple[str, str, str]
) -> None:
    with immediate_transaction(auth_engine) as connection:
        repository = AuthRepository(connection)
        caller = _caller(repository, scoped_library[0], CallerKind.OPERATOR)
        issuer = CredentialIssuer(repository, clock=lambda: _CREATED)
        with pytest.raises(CredentialExpiryError):
            issuer.issue(caller, expires_at=None)
        finite = issuer.issue(caller, expires_at=_CREATED + 10)
        assert (
            AuthenticationService(repository, clock=lambda: _CREATED + 9)
            .authenticate(finite.value)
            .credential.expires_at
            == _CREATED + 10
        )
        with pytest.raises(AuthenticationError):
            AuthenticationService(repository, clock=lambda: _CREATED + 10).authenticate(
                finite.value
            )
        connection.execute(
            update(Credential).where(Credential.id == finite.credential.id).values(expires_at=None)
        )
        with pytest.raises(AuthenticationError):
            AuthenticationService(repository, clock=lambda: _CREATED + 9).authenticate(finite.value)
