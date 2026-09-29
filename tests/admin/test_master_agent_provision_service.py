"""Master-session Agent issuance stays scoped to exact Library grants."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, func, select

import patchouli_lib.admin.service as admin_service_module
from patchouli_lib.admin.contracts import (
    MasterLibraryGrantInput,
    MasterProvisionAgentInput,
    MasterRotateAgentCredentialInput,
    MasterSetAgentLibraryGrantsInput,
)
from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_token_store import MasterTokenRepository
from patchouli_lib.admin.service import AdminActionService, GrantVersionConflictError
from patchouli_lib.admin.session import MasterAdminSession
from patchouli_lib.auth.library_policy import (
    LegacySectionPolicy,
    LibraryAction,
    LibraryGrantPolicy,
    target_library_grants_digest,
)
from patchouli_lib.auth.models import (
    AgentTokenValue,
    Caller,
    Credential,
    CredentialLibraryGrant,
    CredentialLibraryPolicy,
    MasterAuditEvent,
)
from patchouli_lib.auth.repository import AuthRepository
from patchouli_lib.auth.schemas import CallerKind
from patchouli_lib.auth.service import (
    AuthenticationError,
    AuthenticationService,
    CredentialIssuer,
)
from patchouli_lib.database import build_engine, immediate_transaction
from patchouli_lib.library.repository import LibraryRepository
from patchouli_lib.library.schemas import CreateLibraryInput
from patchouli_lib.library.service import LibraryStructureService
from patchouli_lib.operator.service import (
    CredentialLifecycleError,
    PolicyConflictError,
    ResourceNotFoundError,
)


@pytest.fixture
def master_provision_context(
    tmp_path: Path,
) -> Iterator[tuple[AdminActionService, Engine, MasterAdminSession, str, str]]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'master-provision.db').as_posix()}")
    Caller.metadata.create_all(engine)
    with immediate_transaction(engine) as connection:
        libraries = LibraryStructureService(LibraryRepository(connection), clock=lambda: 1_000_000)
        home = libraries.create_library(CreateLibraryInput(name="Synthetic Home"))
        target = libraries.create_library(CreateLibraryInput(name="Synthetic Target"))
        MasterTokenRepository(
            connection, identity_factory=lambda: "a" * 32
        ).initialize_from_local_cli("synthetic master token material 0001", now=1_000_000)
    session = MasterAdminSession(
        expires_at=int(time()) + 600,
        csrf_token="synthetic_csrf_token",
        identity_id="a" * 32,
        session_generation=1,
    )
    service = AdminActionService(engine, clock=lambda: 2_000_000)
    try:
        yield service, engine, session, home.id, target.id
    finally:
        engine.dispose()


def _request(home_id: str, *grants: tuple[str, LibraryAction]) -> MasterProvisionAgentInput:
    return MasterProvisionAgentInput(
        home_library_id=home_id,
        agent_name="Synthetic Device",
        agent_description="Synthetic device description",
        credential_ttl_seconds=3600,
        grants=tuple(
            MasterLibraryGrantInput(library_id=library_id, action=action)
            for library_id, action in grants
        ),
    )


def _grant_edit(
    home_id: str,
    caller_id: str,
    credential_id: str,
    target_id: str,
    *existing: LibraryAction,
    read: bool = False,
    write: bool = False,
    revision: int = 0,
) -> MasterSetAgentLibraryGrantsInput:
    return MasterSetAgentLibraryGrantsInput(
        expected_digest=target_library_grants_digest(
            home_id, caller_id, credential_id, target_id, existing, revision
        ),
        read=read,
        write=write,
    )


def test_master_provision_issues_revealable_token_with_only_explicit_grants(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    result = service.provision_agent_as_master(
        _request(home_id, (home_id, LibraryAction.READ), (target_id, LibraryAction.WRITE)),
        master_session=session,
    )
    assert result.library_id == home_id
    assert result.value.startswith("plb1.")
    assert result.value not in repr(result)
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        authenticated = AuthenticationService(repository, clock=lambda: 2_000_001).authenticate(
            result.value
        )
        assert authenticated.caller.kind is CallerKind.AGENT
        assert authenticated.caller.id == result.caller_id
        assert repository.has_library_grant_policy(home_id, result.caller_id, result.credential_id)
        home_policy = repository.get_library_policy(
            credential_id=result.credential_id,
            caller_id=result.caller_id,
            home_library_id=home_id,
            target_library_id=home_id,
            active_at=2_000_001,
        )
        target_policy = repository.get_library_policy(
            credential_id=result.credential_id,
            caller_id=result.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        )
        assert home_policy == LibraryGrantPolicy(read=True, write=False)
        assert target_policy == LibraryGrantPolicy(read=False, write=True)
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1
        audit = connection.execute(
            select(
                MasterAuditEvent.action, MasterAuditEvent.target_type, MasterAuditEvent.target_id
            )
        ).one()
        assert tuple(audit) == ("auth.agent.provision", "caller", result.caller_id)


def test_master_provision_with_no_grants_is_explicitly_default_deny(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, _ = master_provision_context
    result = service.provision_agent_as_master(_request(home_id), master_session=session)
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        policy = repository.get_library_policy(
            credential_id=result.credential_id,
            caller_id=result.caller_id,
            home_library_id=home_id,
            target_library_id=home_id,
            active_at=2_000_001,
        )
        assert policy == LibraryGrantPolicy(read=False, write=False)
        assert connection.scalar(select(func.count()).select_from(CredentialLibraryGrant)) == 0


def test_master_provision_rejects_duplicate_or_unknown_grants_without_persistence(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, _ = master_provision_context
    with pytest.raises(ValidationError):
        _request(home_id, (home_id, LibraryAction.READ), (home_id, LibraryAction.READ))
    with pytest.raises(ResourceNotFoundError):
        service.provision_agent_as_master(
            _request(home_id, ("f" * 32, LibraryAction.READ)), master_session=session
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Caller)) == 0
        assert connection.scalar(select(func.count()).select_from(Credential)) == 0
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 0
        assert connection.scalar(select(func.count()).select_from(CredentialLibraryPolicy)) == 0
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 0


def test_master_provision_rolls_back_secret_and_identity_if_audit_fails(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine, session, home_id, _ = master_provision_context

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        service.provision_agent_as_master(
            _request(home_id, (home_id, LibraryAction.READ)), master_session=session
        )
    with engine.connect() as connection:
        for table in (
            Caller,
            Credential,
            AgentTokenValue,
            CredentialLibraryPolicy,
            CredentialLibraryGrant,
            MasterAuditEvent,
        ):
            assert connection.scalar(select(func.count()).select_from(table)) == 0


def test_master_provision_works_with_current_migrated_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'migrated-provision.db').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        with immediate_transaction(engine) as connection:
            home = LibraryStructureService(
                LibraryRepository(connection), clock=lambda: 1_000_000
            ).create_library(CreateLibraryInput(name="Migrated Home"))
            MasterTokenRepository(
                connection, identity_factory=lambda: "a" * 32
            ).initialize_from_local_cli("synthetic master token material 0001", now=1_000_000)
        session = MasterAdminSession(
            expires_at=int(time()) + 600,
            csrf_token="synthetic_csrf_token",
            identity_id="a" * 32,
            session_generation=1,
        )
        issued = AdminActionService(engine, clock=lambda: 2_000_000).provision_agent_as_master(
            _request(home.id, (home.id, LibraryAction.READ)), master_session=session
        )
        replacement = AdminActionService(
            engine, clock=lambda: 2_000_001
        ).rotate_agent_credential_as_master(
            home.id,
            issued.caller_id,
            issued.credential_id,
            MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
            master_session=session,
        )
        with engine.connect() as connection:
            repository = AuthRepository(connection)
            assert repository.has_library_grant_policy(
                home.id, issued.caller_id, replacement.credential_id
            )
            assert (
                AuthenticationService(repository, clock=lambda: 2_000_002)
                .authenticate(replacement.value)
                .caller.id
                == issued.caller_id
            )
            with pytest.raises(AuthenticationError):
                AuthenticationService(repository, clock=lambda: 2_000_002).authenticate(
                    issued.value
                )
    finally:
        engine.dispose()


def test_master_rotation_preserves_exact_grants_and_revokes_only_selected_token(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    first = service.provision_agent_as_master(
        _request(home_id, (home_id, LibraryAction.READ), (target_id, LibraryAction.WRITE)),
        master_session=session,
    )
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home_id, first.caller_id)
        assert caller is not None
        sibling = CredentialIssuer(repository, clock=lambda: 2_000_000).issue(
            caller, expires_at=2_000_000 + 3_600_000_000
        )
    replacement = service.rotate_agent_credential_as_master(
        home_id,
        first.caller_id,
        first.credential_id,
        MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
        master_session=session,
    )
    assert replacement.caller_id == first.caller_id
    assert replacement.credential_id != first.credential_id
    assert replacement.value != first.value
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        with pytest.raises(AuthenticationError):
            AuthenticationService(repository, clock=lambda: 2_000_001).authenticate(first.value)
        assert (
            AuthenticationService(repository, clock=lambda: 2_000_001)
            .authenticate(replacement.value)
            .caller.id
            == first.caller_id
        )
        assert (
            AuthenticationService(repository, clock=lambda: 2_000_001)
            .authenticate(sibling.value)
            .caller.id
            == first.caller_id
        )
        stored_ids = set(connection.scalars(select(AgentTokenValue.credential_id)))
        assert first.credential_id not in stored_ids
        assert stored_ids == {replacement.credential_id, sibling.credential.id}
        assert (
            repository.get_active_agent_token_value(
                home_id, first.caller_id, first.credential_id, active_at=2_000_001
            )
            is None
        )
        assert (
            repository.get_active_agent_token_value(
                home_id, first.caller_id, replacement.credential_id, active_at=2_000_001
            )
            == replacement.value
        )
        assert repository.has_library_grant_policy(
            home_id, first.caller_id, replacement.credential_id
        )
        assert repository.list_credential_library_grants(
            home_library_id=home_id,
            caller_id=first.caller_id,
            credential_id=replacement.credential_id,
        ) == repository.list_credential_library_grants(
            home_library_id=home_id, caller_id=first.caller_id, credential_id=first.credential_id
        )
        old = repository.get_credential(home_id, first.caller_id, first.credential_id)
        assert old is not None
        assert old.rotated_to_credential_id == replacement.credential_id
        assert connection.scalars(select(MasterAuditEvent.action)).all() == [
            "auth.agent.provision",
            "auth.agent_credential.rotate",
        ]
    with pytest.raises(CredentialLifecycleError):
        service.rotate_agent_credential_as_master(
            home_id,
            first.caller_id,
            first.credential_id,
            MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
            master_session=session,
        )


def test_master_rotation_preserves_explicit_zero_grants_and_rolls_back_on_audit_failure(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine, session, home_id, _ = master_provision_context
    first = service.provision_agent_as_master(_request(home_id), master_session=session)

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic rotation audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    with pytest.raises(RuntimeError, match="synthetic rotation audit failure"):
        service.rotate_agent_credential_as_master(
            home_id,
            first.caller_id,
            first.credential_id,
            MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
            master_session=session,
        )
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1
        assert (
            repository.get_active_agent_token_value(
                home_id, first.caller_id, first.credential_id, active_at=2_000_001
            )
            == first.value
        )
        assert repository.has_library_grant_policy(home_id, first.caller_id, first.credential_id)
        assert connection.scalar(select(func.count()).select_from(CredentialLibraryGrant)) == 0
    monkeypatch.undo()
    replacement = service.rotate_agent_credential_as_master(
        home_id,
        first.caller_id,
        first.credential_id,
        MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
        master_session=session,
    )
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert repository.has_library_grant_policy(
            home_id, first.caller_id, replacement.credential_id
        )
        assert repository.get_library_policy(
            credential_id=replacement.credential_id,
            caller_id=first.caller_id,
            home_library_id=home_id,
            target_library_id=home_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=False, write=False)


def test_master_rotation_rechecks_expiry_after_acquiring_write_transaction(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine, session, home_id, _ = master_provision_context
    first = service.provision_agent_as_master(
        _request(home_id, (home_id, LibraryAction.READ)), master_session=session
    )
    with engine.connect() as connection:
        current = AuthRepository(connection).get_credential(
            home_id, first.caller_id, first.credential_id
        )
        assert current is not None
        expires_at = current.expires_at
    observed_time = [expires_at - 1]
    late_service = AdminActionService(engine, clock=lambda: observed_time[0])
    original_transaction = immediate_transaction

    @contextmanager
    def lock_acquired_late(target_engine: Engine) -> Iterator[Any]:
        with original_transaction(target_engine) as connection:
            observed_time[0] = expires_at
            yield connection

    monkeypatch.setattr(admin_service_module, "immediate_transaction", lock_acquired_late)
    with pytest.raises(CredentialLifecycleError):
        late_service.rotate_agent_credential_as_master(
            home_id,
            first.caller_id,
            first.credential_id,
            MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
            master_session=session,
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Credential)) == 1
        assert connection.scalar(select(func.count()).select_from(AgentTokenValue)) == 1


def test_master_grant_edit_changes_only_exact_token_and_target_library(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    issued = service.provision_agent_as_master(
        _request(home_id, (home_id, LibraryAction.READ), (target_id, LibraryAction.WRITE)),
        master_session=session,
    )
    with immediate_transaction(engine) as connection:
        repository = AuthRepository(connection)
        caller = repository.get_caller(home_id, issued.caller_id)
        assert caller is not None
        legacy_sibling = CredentialIssuer(repository, clock=lambda: 2_000_000).issue(
            caller, expires_at=2_000_000 + 3_600_000_000
        )
    changed = service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        _grant_edit(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            LibraryAction.WRITE,
            read=True,
        ),
        master_session=session,
    )
    assert changed is True
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert repository.get_library_policy(
            credential_id=issued.credential_id,
            caller_id=issued.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=True, write=False)
        assert repository.get_library_policy(
            credential_id=issued.credential_id,
            caller_id=issued.caller_id,
            home_library_id=home_id,
            target_library_id=home_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=True, write=False)
        assert (
            repository.get_library_policy(
                credential_id=legacy_sibling.credential.id,
                caller_id=issued.caller_id,
                home_library_id=home_id,
                target_library_id=target_id,
                active_at=2_000_001,
            )
            == LegacySectionPolicy()
        )
        audit = connection.execute(
            select(
                MasterAuditEvent.action, MasterAuditEvent.target_type, MasterAuditEvent.target_id
            ).where(MasterAuditEvent.action == "auth.agent_credential.grants_update")
        ).one()
        assert tuple(audit) == (
            "auth.agent_credential.grants_update",
            "credential_library_grant",
            f"{issued.credential_id}:{target_id}:01:10",
        )
    with pytest.raises(PolicyConflictError):
        service.set_agent_grants_as_master(
            home_id,
            issued.caller_id,
            legacy_sibling.credential.id,
            target_id,
            _grant_edit(
                home_id, issued.caller_id, legacy_sibling.credential.id, target_id, read=True
            ),
            master_session=session,
        )
    assert service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        _grant_edit(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            LibraryAction.READ,
            revision=1,
        ),
        master_session=session,
    )
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert repository.has_library_grant_policy(home_id, issued.caller_id, issued.credential_id)
        assert repository.get_library_policy(
            credential_id=issued.credential_id,
            caller_id=issued.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=False, write=False)


def test_master_grant_edit_rejects_stale_form_and_rolls_back_failed_audit(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    issued = service.provision_agent_as_master(_request(home_id), master_session=session)
    original = _grant_edit(home_id, issued.caller_id, issued.credential_id, target_id, read=True)
    assert service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        original,
        master_session=session,
    )
    with pytest.raises(GrantVersionConflictError):
        service.set_agent_grants_as_master(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            original,
            master_session=session,
        )
    current = _grant_edit(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        LibraryAction.READ,
        read=True,
        revision=1,
    )
    assert not service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        current,
        master_session=session,
    )

    def reject_audit(self: MasterAuditRepository, **kwargs: Any) -> None:
        raise RuntimeError("synthetic grant audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    with pytest.raises(RuntimeError, match="synthetic grant audit failure"):
        service.set_agent_grants_as_master(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            _grant_edit(
                home_id,
                issued.caller_id,
                issued.credential_id,
                target_id,
                LibraryAction.READ,
                write=True,
                revision=1,
            ),
            master_session=session,
        )
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert repository.get_library_policy(
            credential_id=issued.credential_id,
            caller_id=issued.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=True, write=False)
        assert (
            connection.execute(
                select(func.count())
                .select_from(MasterAuditEvent)
                .where(MasterAuditEvent.action == "auth.agent_credential.grants_update")
            ).scalar_one()
            == 1
        )


def test_master_grant_edit_rejects_aba_even_when_bits_return_to_original_value(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    issued = service.provision_agent_as_master(_request(home_id), master_session=session)
    stale_form = _grant_edit(home_id, issued.caller_id, issued.credential_id, target_id, read=True)
    assert service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        stale_form,
        master_session=session,
    )
    assert service.set_agent_grants_as_master(
        home_id,
        issued.caller_id,
        issued.credential_id,
        target_id,
        _grant_edit(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            LibraryAction.READ,
            revision=1,
        ),
        master_session=session,
    )
    with pytest.raises(GrantVersionConflictError):
        service.set_agent_grants_as_master(
            home_id,
            issued.caller_id,
            issued.credential_id,
            target_id,
            stale_form,
            master_session=session,
        )
    with engine.connect() as connection:
        repository = AuthRepository(connection)
        assert repository.get_library_policy(
            credential_id=issued.credential_id,
            caller_id=issued.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=False, write=False)
        assert (
            connection.execute(
                select(func.count())
                .select_from(MasterAuditEvent)
                .where(MasterAuditEvent.action == "auth.agent_credential.grants_update")
            ).scalar_one()
            == 2
        )


def test_master_grant_edit_and_rotation_serialize_without_reviving_old_token(
    master_provision_context: tuple[AdminActionService, Engine, MasterAdminSession, str, str],
) -> None:
    service, engine, session, home_id, target_id = master_provision_context
    first = service.provision_agent_as_master(_request(home_id), master_session=session)
    assert service.set_agent_grants_as_master(
        home_id,
        first.caller_id,
        first.credential_id,
        target_id,
        _grant_edit(home_id, first.caller_id, first.credential_id, target_id, write=True),
        master_session=session,
    )
    replacement = service.rotate_agent_credential_as_master(
        home_id,
        first.caller_id,
        first.credential_id,
        MasterRotateAgentCredentialInput(credential_ttl_seconds=7200),
        master_session=session,
    )
    with engine.connect() as connection:
        assert AuthRepository(connection).get_library_policy(
            credential_id=replacement.credential_id,
            caller_id=first.caller_id,
            home_library_id=home_id,
            target_library_id=target_id,
            active_at=2_000_001,
        ) == LibraryGrantPolicy(read=False, write=True)
    with pytest.raises(CredentialLifecycleError):
        service.set_agent_grants_as_master(
            home_id,
            first.caller_id,
            first.credential_id,
            target_id,
            _grant_edit(
                home_id,
                first.caller_id,
                first.credential_id,
                target_id,
                LibraryAction.WRITE,
                read=True,
            ),
            master_session=session,
        )
