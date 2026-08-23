"""Tests for the administrative web action service."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, func, select

from patchouli_lib.admin.contracts import (
    BootstrapInput,
    ProvisionAgentInput,
    RecoverOperatorInput,
    RevokeAgentCredentialInput,
)
from patchouli_lib.admin.service import AdminActionService
from patchouli_lib.auth.models import AuditEvent, Caller, Credential, SectionGrant
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind, SectionAction
from patchouli_lib.auth.service import AuthenticationError, AuthenticationService
from patchouli_lib.database import build_engine
from patchouli_lib.operator.service import ResourceNotFoundError

_LIBRARY_NAME = "Synthetic Admin Library"
_SECTION_NAME = "Synthetic Admin Section"
_BOOK_NAME = "Synthetic Admin Book"


@pytest.fixture
def admin_service(tmp_path: Path) -> Iterator[tuple[AdminActionService, Engine]]:
    database_path = tmp_path / "admin-service.db"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Caller.metadata.create_all(engine)
    request_counter = iter(range(1, 100))
    service = AdminActionService(
        engine,
        clock=lambda: 1_000_000,
        request_id_factory=lambda: f"req_admin_{next(request_counter)}",
    )
    try:
        yield service, engine
    finally:
        engine.dispose()


def _bootstrap_input() -> BootstrapInput:
    return BootstrapInput(
        library_name=_LIBRARY_NAME,
        section_name=_SECTION_NAME,
        section_description="Synthetic description",
        book_name=_BOOK_NAME,
        book_summary="Synthetic summary",
        operator_name="Synthetic Admin Operator",
        operator_description="Synthetic operator",
        credential_ttl_seconds=60,
    )


def test_bootstrap_and_recovery_reuse_existing_domain_transactions(
    admin_service: tuple[AdminActionService, Engine],
) -> None:
    service, engine = admin_service

    bootstrapped = service.bootstrap(_bootstrap_input())
    recovered = service.recover_operator(
        RecoverOperatorInput(
            library_name=_LIBRARY_NAME,
            credential_ttl_seconds=120,
        )
    )

    assert bootstrapped.value.startswith("plb1.")
    assert bootstrapped.value not in repr(bootstrapped)
    assert recovered.value.startswith("plb1.")
    assert recovered.value not in repr(recovered)
    assert recovered.caller_id == bootstrapped.caller_id
    assert recovered.credential_id != bootstrapped.credential_id

    with engine.connect() as connection:
        repository = AuthRepository(connection)
        with pytest.raises(AuthenticationError):
            AuthenticationService(repository, clock=lambda: 2_000_000).authenticate(
                bootstrapped.value
            )
        authenticated = AuthenticationService(
            repository,
            clock=lambda: 2_000_000,
        ).authenticate(recovered.value)
    assert authenticated.caller.kind is CallerKind.OPERATOR


def test_provision_and_revoke_agent_are_audited_and_exactly_scoped(
    admin_service: tuple[AdminActionService, Engine],
) -> None:
    service, engine = admin_service
    operator = service.bootstrap(_bootstrap_input())

    agent = service.provision_agent(
        ProvisionAgentInput(
            library_name=_LIBRARY_NAME,
            section_name=_SECTION_NAME,
            agent_name="Synthetic Agent",
            agent_description="Synthetic caller",
            credential_ttl_seconds=120,
            grants=(SectionAction.QUERY, SectionAction.ARCHIVE_WRITE),
            operator_token=SecretStr(operator.value),
        )
    )

    assert agent.value.startswith("plb1.")
    assert agent.value not in repr(agent)
    with engine.connect() as connection:
        grant_actions = tuple(
            connection.scalars(
                select(SectionGrant.action)
                .where(SectionGrant.caller_id == agent.caller_id)
                .order_by(SectionGrant.action)
            )
        )
        actions = tuple(
            connection.scalars(select(AuditEvent.action).order_by(AuditEvent.occurred_at))
        )
    assert grant_actions == ("archive:write", "section:query")
    assert "auth.caller.create" in actions
    assert "auth.credential.create" in actions
    assert actions.count("auth.grant.add") == 2

    service.revoke_agent_credential(
        RevokeAgentCredentialInput(
            library_name=_LIBRARY_NAME,
            caller_id=agent.caller_id,
            credential_id=agent.credential_id,
            operator_token=SecretStr(operator.value),
        )
    )

    with engine.connect() as connection, pytest.raises(AuthenticationError):
        AuthenticationService(
            AuthRepository(connection),
            clock=lambda: 2_000_000,
        ).authenticate(agent.value)


def test_failed_provision_rolls_back_agent_and_credential(
    admin_service: tuple[AdminActionService, Engine],
) -> None:
    service, engine = admin_service
    service.bootstrap(_bootstrap_input())

    with pytest.raises(AuthenticationError):
        service.provision_agent(
            ProvisionAgentInput(
                library_name=_LIBRARY_NAME,
                section_name=_SECTION_NAME,
                agent_name="Rolled Back Agent",
                credential_ttl_seconds=120,
                grants=(SectionAction.QUERY,),
                operator_token=SecretStr("plb1.invalid"),
            )
        )

    with engine.connect() as connection:
        agent_count = connection.scalar(
            select(func.count()).select_from(Caller).where(Caller.kind == CallerKind.AGENT.value)
        )
        credential_count = connection.scalar(select(func.count()).select_from(Credential))
    assert agent_count == 0
    assert credential_count == 1


def test_revoke_rejects_operator_or_missing_resource(
    admin_service: tuple[AdminActionService, Engine],
) -> None:
    service, _ = admin_service
    operator = service.bootstrap(_bootstrap_input())

    with pytest.raises(ResourceNotFoundError):
        service.revoke_agent_credential(
            RevokeAgentCredentialInput(
                library_name=_LIBRARY_NAME,
                caller_id=operator.caller_id,
                credential_id=operator.credential_id,
                operator_token=SecretStr(operator.value),
            )
        )
    with pytest.raises(ResourceNotFoundError):
        service.recover_operator(
            RecoverOperatorInput(
                library_name="Missing Library",
                credential_ttl_seconds=60,
            )
        )


def test_expiry_overflow_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "admin-overflow.db"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Caller.metadata.create_all(engine)
    service = AdminActionService(
        engine,
        clock=lambda: 253_402_300_799_000_000,
        request_id_factory=lambda: "req_admin_overflow",
    )
    try:
        with pytest.raises(ValueError, match="expiry"):
            service.bootstrap(_bootstrap_input())
    finally:
        engine.dispose()
