from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from patchouli_lib.admin.master_token_store import (
    MasterTokenAlreadyInitialized,
    MasterTokenRepository,
)
from patchouli_lib.admin.passwords import parse_password_hash
from patchouli_lib.auth.models import Caller, MasterIdentity
from patchouli_lib.auth.tokens import generate_token
from patchouli_lib.database import build_engine, immediate_transaction

OLD_TOKEN = "synthetic master token material old 0001"
NEW_TOKEN = "synthetic master token material new 0002"
IDENTITY = "a" * 32


@pytest.fixture
def master_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = build_engine(f"sqlite:///{(tmp_path / 'master.db').as_posix()}")
    Caller.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_first_setup_stores_only_salted_verifier_and_one_identity(master_engine: Engine) -> None:
    with immediate_transaction(master_engine) as connection:
        repository = MasterTokenRepository(connection, identity_factory=lambda: IDENTITY)
        assert not repository.has_identity()
        state = repository.initialize_from_local_cli(OLD_TOKEN, now=1_000)
        assert repository.has_identity()
        assert state.identity_id == IDENTITY
        assert state.session_generation == 1
        assert repository.authenticate(OLD_TOKEN) == state
        assert repository.authenticate(NEW_TOKEN) is None
        assert repository.authenticate(generate_token().value) is None
        assert repository.is_session_generation_current(IDENTITY, 1)
        assert not repository.is_session_generation_current(IDENTITY, 2)
        assert not repository.is_session_generation_current(IDENTITY, True)
        assert not repository.is_session_generation_current("b" * 32, 1)
        verifier = connection.execute(select(MasterIdentity.token_verifier)).scalar_one()
        assert OLD_TOKEN not in verifier
        _, salt, _ = parse_password_hash(verifier)
        assert len(salt) == 16
        with pytest.raises(MasterTokenAlreadyInitialized) as error:
            repository.initialize_from_local_cli(NEW_TOKEN, now=1_001)
        assert OLD_TOKEN not in repr(error.value)
        assert NEW_TOKEN not in repr(error.value)
    with master_engine.connect() as connection:
        assert connection.execute(select(MasterIdentity.slot)).scalars().all() == [1]


def test_rotation_rejects_old_token_and_old_session_generation(master_engine: Engine) -> None:
    with immediate_transaction(master_engine) as connection:
        repository = MasterTokenRepository(connection, identity_factory=lambda: IDENTITY)
        first = repository.initialize_from_local_cli(OLD_TOKEN, now=1_000)
        old_verifier = connection.execute(select(MasterIdentity.token_verifier)).scalar_one()
        assert (
            repository.rotate("wrong old token material 00000000000", NEW_TOKEN, now=1_001) is None
        )
        with pytest.raises(ValueError, match="must differ"):
            repository.rotate(OLD_TOKEN, OLD_TOKEN, now=1_001)
        second = repository.rotate(OLD_TOKEN, NEW_TOKEN, now=1_001)
        assert second is not None
        assert second.identity_id == first.identity_id
        assert second.session_generation == first.session_generation + 1
        assert repository.authenticate(OLD_TOKEN) is None
        assert repository.authenticate(NEW_TOKEN) == second
        assert repository.rotate(OLD_TOKEN, OLD_TOKEN, now=1_002) is None
        assert not repository.is_session_generation_current(IDENTITY, first.session_generation)
        assert repository.is_session_generation_current(IDENTITY, second.session_generation)
        new_verifier = connection.execute(select(MasterIdentity.token_verifier)).scalar_one()
        assert old_verifier != new_verifier
        assert parse_password_hash(old_verifier)[1] != parse_password_hash(new_verifier)[1]


def test_invalid_inputs_fail_closed_without_echo(master_engine: Engine) -> None:
    with immediate_transaction(master_engine) as connection:
        repository = MasterTokenRepository(connection, identity_factory=lambda: IDENTITY)
        with pytest.raises(ValueError) as short_error:
            repository.initialize_from_local_cli("short", now=1_000)
        assert "short" not in repr(short_error.value)
        with pytest.raises(ValueError) as unicode_error:
            repository.initialize_from_local_cli("x" * 32 + "\ud800", now=1_000)
        assert "\ud800" not in repr(unicode_error.value)
        assert repository.authenticate(OLD_TOKEN) is None
        repository.initialize_from_local_cli(OLD_TOKEN, now=1_000)
        assert repository.authenticate("x" * 32 + "\ud800") is None
        with pytest.raises(ValueError):
            repository.rotate(OLD_TOKEN, "too short", now=1_001)
        assert repository.authenticate(OLD_TOKEN) is not None
