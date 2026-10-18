from __future__ import annotations

import pytest
from sqlalchemy import Engine, insert, update
from sqlalchemy.exc import IntegrityError

from patchouli_lib.auth.library_policy import (
    LegacySectionPolicy,
    LibraryAction,
    LibraryGrantPolicy,
    LibraryPolicy,
)
from patchouli_lib.auth.models import (
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
)
from patchouli_lib.auth.repository import AuthRepository, CredentialLibraryScope
from patchouli_lib.auth.schemas import CallerKind, NewCaller, NewCredential
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.library.models import Library

HOME = "1" * 32
OTHER = "2" * 32
FUTURE = "3" * 32
CALLER = "4" * 32
CREDENTIAL = "5" * 32


def _add_library(engine: Engine, library_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(Library),
            {"id": library_id, "name": library_id, "created_at": 10, "updated_at": 10},
        )


def _seed(engine: Engine, *, modes: tuple[str, str] | None = ("include", "include")) -> None:
    _add_library(engine, HOME)
    _add_library(engine, OTHER)
    token = generate_token()
    with engine.begin() as connection:
        repository = AuthRepository(connection)
        repository.add_caller(
            NewCaller(
                id=CALLER,
                library_id=HOME,
                name="Synthetic Scope Agent",
                kind=CallerKind.AGENT,
                created_at=10,
                updated_at=10,
            )
        )
        repository.add_credential(
            NewCredential(
                id=CREDENTIAL,
                caller_id=CALLER,
                library_id=HOME,
                selector=token.selector,
                token_version=token.version,
                verifier=token.verifier,
                expires_at=100,
                created_at=10,
                updated_at=10,
            )
        )
        if modes is not None:
            connection.execute(
                insert(CredentialLibraryPolicy),
                {
                    "credential_id": CREDENTIAL,
                    "caller_id": CALLER,
                    "home_library_id": HOME,
                    "mode": "library_grants",
                    "read_mode": modes[0],
                    "write_mode": modes[1],
                    "scope_revision": 7,
                    "created_at": 10,
                },
            )


def _select(engine: Engine, library_id: str, action: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(CredentialLibraryGrant),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": HOME,
                "target_library_id": library_id,
                "action": action,
                "created_at": 10,
            },
        )


def _resolve(engine: Engine, library_id: str, *, active_at: int = 20) -> LibraryPolicy | None:
    with engine.connect() as connection:
        return AuthRepository(connection).get_library_policy(
            credential_id=CREDENTIAL,
            caller_id=CALLER,
            home_library_id=HOME,
            target_library_id=library_id,
            active_at=active_at,
        )


@pytest.mark.parametrize("read_mode", ["include", "exclude"])
@pytest.mark.parametrize("write_mode", ["include", "exclude"])
def test_modes_independent_and_new_libraries_follow_policy(
    auth_engine: Engine,
    read_mode: str,
    write_mode: str,
) -> None:
    _seed(auth_engine, modes=(read_mode, write_mode))
    _select(auth_engine, HOME, "read")
    _select(auth_engine, OTHER, "write")
    assert _resolve(auth_engine, HOME) == LibraryGrantPolicy(
        read=read_mode == "include",
        write=write_mode == "exclude",
    )
    assert _resolve(auth_engine, OTHER) == LibraryGrantPolicy(
        read=read_mode == "exclude",
        write=write_mode == "include",
    )
    assert _resolve(auth_engine, FUTURE) == LibraryGrantPolicy(read=False, write=False)
    _add_library(auth_engine, FUTURE)
    assert _resolve(auth_engine, FUTURE) == LibraryGrantPolicy(
        read=read_mode == "exclude",
        write=write_mode == "exclude",
    )
    with auth_engine.connect() as connection:
        repository = AuthRepository(connection)
        scope = repository.get_credential_library_scope(HOME, CALLER, CREDENTIAL)
        assert scope == CredentialLibraryScope(read_mode, write_mode, (HOME,), (OTHER,), 7)
        raw = repository.list_credential_library_selections(
            home_library_id=HOME,
            caller_id=CALLER,
            credential_id=CREDENTIAL,
        )
        assert [(item.library_id, item.actions) for item in raw] == [
            (HOME, (LibraryAction.READ,)),
            (OTHER, (LibraryAction.WRITE,)),
        ]
        effective = repository.list_credential_library_grants(
            home_library_id=HOME,
            caller_id=CALLER,
            credential_id=CREDENTIAL,
        )
        assert [item.library_id for item in effective] == sorted(
            item.library_id for item in effective
        )
        effective_by_id = {item.library_id: item.actions for item in effective}
        for library_id in (HOME, OTHER, FUTURE):
            policy = _resolve(auth_engine, library_id)
            assert isinstance(policy, LibraryGrantPolicy)
            assert (LibraryAction.READ in effective_by_id.get(library_id, ())) == policy.read
            assert (LibraryAction.WRITE in effective_by_id.get(library_id, ())) == policy.write


def test_all_libraries_empty_exclusion_and_legacy_absence(auth_engine: Engine) -> None:
    _seed(auth_engine, modes=None)
    assert isinstance(_resolve(auth_engine, HOME), LegacySectionPolicy)
    with auth_engine.begin() as connection:
        repository = AuthRepository(connection)
        assert repository.get_credential_library_scope(HOME, CALLER, CREDENTIAL) is None
        assert (
            repository.list_credential_library_grants(
                home_library_id=HOME,
                caller_id=CALLER,
                credential_id=CREDENTIAL,
            )
            == ()
        )
        connection.execute(
            insert(CredentialLibraryPolicy),
            {
                "credential_id": CREDENTIAL,
                "caller_id": CALLER,
                "home_library_id": HOME,
                "mode": "library_grants",
                "created_at": 10,
            },
        )
        scope = repository.get_credential_library_scope(HOME, CALLER, CREDENTIAL)
        assert scope == CredentialLibraryScope("include", "include", (), (), 0)
        connection.execute(
            update(CredentialLibraryPolicy).values(read_mode="exclude", write_mode="exclude")
        )
    for library_id in (HOME, OTHER):
        assert _resolve(auth_engine, library_id) == LibraryGrantPolicy(read=True, write=True)
    _add_library(auth_engine, FUTURE)
    assert _resolve(auth_engine, FUTURE) == LibraryGrantPolicy(read=True, write=True)


@pytest.mark.parametrize(
    "mutation", ["revoked", "rotated", "disabled", "operator", "future", "expired"]
)
def test_exclusion_does_not_bypass_identity_checks(auth_engine: Engine, mutation: str) -> None:
    _seed(auth_engine, modes=("exclude", "exclude"))
    with auth_engine.begin() as connection:
        if mutation == "revoked":
            connection.execute(update(Credential).values(revoked_at=20, updated_at=20))
        elif mutation == "rotated":
            token = generate_token()
            AuthRepository(connection).add_credential(
                NewCredential(
                    id="6" * 32,
                    caller_id=CALLER,
                    library_id=HOME,
                    selector=token.selector,
                    token_version=token.version,
                    verifier=token.verifier,
                    expires_at=100,
                    created_at=20,
                    updated_at=20,
                )
            )
            connection.execute(
                update(Credential)
                .where(Credential.id == CREDENTIAL)
                .values(
                    rotated_at=20,
                    revoked_at=20,
                    rotated_to_credential_id="6" * 32,
                    updated_at=20,
                )
            )
        elif mutation == "disabled":
            connection.execute(update(Caller).values(disabled_at=20, updated_at=20))
        elif mutation == "operator":
            connection.execute(update(Caller).values(kind="operator"))
    active_at = 9 if mutation == "future" else 100 if mutation == "expired" else 20
    assert _resolve(auth_engine, OTHER, active_at=active_at) is None


def test_exact_credential_identity_required(auth_engine: Engine) -> None:
    _seed(auth_engine, modes=("exclude", "exclude"))
    with auth_engine.connect() as connection:
        repository = AuthRepository(connection)
        for home, caller, credential in (
            (OTHER, CALLER, CREDENTIAL),
            (HOME, "6" * 32, CREDENTIAL),
            (HOME, CALLER, "7" * 32),
        ):
            assert repository.get_credential_library_scope(home, caller, credential) is None
            assert (
                repository.list_credential_library_grants(
                    home_library_id=home, caller_id=caller, credential_id=credential
                )
                == ()
            )
            assert (
                repository.get_library_policy(
                    home_library_id=home,
                    caller_id=caller,
                    credential_id=credential,
                    target_library_id=OTHER,
                    active_at=20,
                )
                is None
            )


@pytest.mark.parametrize(
    "field,value",
    [("read_mode", "all"), ("write_mode", "all"), ("scope_revision", -1), ("scope_revision", 0.5)],
)
def test_scope_constraints(auth_engine: Engine, field: str, value: str | int | float) -> None:
    _seed(auth_engine)
    with pytest.raises(IntegrityError), auth_engine.begin() as connection:
        connection.execute(update(CredentialLibraryPolicy).values(**{field: value}))
