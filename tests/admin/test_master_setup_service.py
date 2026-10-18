"""Synthetic migrated-database checks for authorized first browser setup."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text

from patchouli_lib.admin.master_audit import MasterAuditRepository
from patchouli_lib.admin.master_setup import (
    MasterSetupAuthorizationError,
    MasterSetupService,
    MasterSetupUnavailableError,
)
from patchouli_lib.admin.master_setup_session import MasterSetupSession, MasterSetupSessionCodec
from patchouli_lib.admin.master_token_store import (
    MasterTokenAlreadyInitialized,
    MasterTokenRepository,
    MasterTokenState,
)
from patchouli_lib.admin.passwords import hash_password
from patchouli_lib.admin.session import AdminSession, AdminSessionCodec
from patchouli_lib.auth.models import Caller, Credential, MasterAuditEvent, MasterIdentity
from patchouli_lib.backup.validation import validate_database
from patchouli_lib.config import Settings
from patchouli_lib.content.models import Page, Revision
from patchouli_lib.database import CURRENT_SCHEMA_REVISION, build_engine, immediate_transaction
from patchouli_lib.library.models import Library

_TOKEN = "synthetic first browser master token 0001"
_OTHER_TOKEN = "synthetic different browser master token 0002"
_PROOF = "synthetic separate first-setup authority 0001"
_SIGNING_SECRET = "s" * 32
_IDENTITY = "a" * 32
_AUDIT = "b" * 32
_PASSWORD_HASH = hash_password("synthetic legacy password", iterations=300_000)


@pytest.fixture
def setup_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    database_url = f"sqlite:///{(tmp_path / 'master-setup.sqlite').as_posix()}"
    monkeypatch.setenv("PATCHOULI_DATABASE_URL", database_url)
    monkeypatch.setenv("PATCHOULI_ENVIRONMENT", "test")
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    engine = build_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _settings(engine: Engine, *, legacy: bool = True, proof: bool = True) -> Settings:
    return Settings(  # type: ignore[call-arg]  # BaseSettings supports _env_file at runtime.
        _env_file=None,
        environment="test",
        database_url=str(engine.url),
        admin_session_signing_secret=SecretStr(_SIGNING_SECRET),
        admin_password_hash=SecretStr(_PASSWORD_HASH) if legacy else None,
        admin_setup_token=SecretStr(_PROOF) if proof else None,
    )


def _codecs(
    clock: Callable[[], float] = lambda: 1_000.0,
) -> tuple[AdminSessionCodec, MasterSetupSessionCodec]:
    return (
        AdminSessionCodec(
            _SIGNING_SECRET.encode(),
            ttl_seconds=300,
            clock=clock,
            token_factory=lambda _: "c" * 43,
        ),
        MasterSetupSessionCodec(
            _SIGNING_SECRET.encode(), clock=clock, token_factory=lambda _: "d" * 43
        ),
    )


def _service(
    engine: Engine,
    *,
    settings: Settings | None = None,
    clock: Callable[[], float] = lambda: 1_000.0,
    identity_id: str = _IDENTITY,
) -> MasterSetupService:
    legacy, setup = _codecs(clock)
    return MasterSetupService(
        engine,
        settings or _settings(engine),
        legacy_session_codec=legacy,
        setup_session_codec=setup,
        clock=clock,
        identity_factory=lambda: identity_id,
        event_id_factory=lambda: _AUDIT,
    )


def _empty(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 0
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 0


@pytest.mark.parametrize("mode", ["legacy", "proof"])
def test_first_setup_is_authorized_and_atomic_without_other_entities(
    setup_engine: Engine, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_cli(*args: object, **kwargs: object) -> MasterTokenState:
        del args, kwargs
        raise AssertionError("Browser setup must not call the local CLI wrapper.")

    monkeypatch.setattr(MasterTokenRepository, "initialize_from_local_cli", reject_cli)
    legacy, setup = _codecs()
    session: AdminSession | MasterSetupSession
    if mode == "legacy":
        cookie, session = legacy.issue()
        state = _service(setup_engine).initialize(_TOKEN, _TOKEN, legacy_cookie=cookie)
    else:
        cookie, session = setup.issue()
        state = _service(setup_engine).initialize(
            _TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie
        )
    assert state == MasterTokenState(_IDENTITY, 1)
    with setup_engine.connect() as connection:
        repository = MasterTokenRepository(connection)
        assert repository.authenticate(_TOKEN) == state
        assert repository.authenticate(_OTHER_TOKEN) is None
        identity = connection.execute(select(MasterIdentity.__table__)).mappings().one()
        assert identity["created_at"] == identity["updated_at"] == 1_000_000_000
        assert _TOKEN not in identity["token_verifier"]
        audit = connection.execute(select(MasterAuditEvent.__table__)).mappings().one()
        assert dict(audit) == {
            "id": _AUDIT,
            "identity_id": _IDENTITY,
            "session_generation": 1,
            "session_fingerprint": session.audit_fingerprint(),
            "action": "auth.master.initialize",
            "target_type": "master_identity",
            "target_id": _IDENTITY,
            "occurred_at": 1_000_000_000,
        }
        assert all(
            secret not in repr(audit) for secret in (_TOKEN, _PROOF, cookie, session.csrf_token)
        )
        for model in (Caller, Credential, Library, Page, Revision):
            assert connection.scalar(select(func.count()).select_from(model)) == 0
    database_path = setup_engine.url.database
    assert database_path is not None
    assert validate_database(Path(database_path)).schema_revision == CURRENT_SCHEMA_REVISION


@pytest.mark.parametrize(
    "case",
    [
        "empty_authority",
        "missing_proof",
        "missing_setup_cookie",
        "wrong_proof",
        "invalid_unicode_proof",
        "invalid_setup_cookie",
        "legacy_cookie_as_setup",
        "master_cookie_as_setup",
        "setup_cookie_as_legacy",
        "master_cookie_as_legacy",
        "legacy_disabled",
        "proof_disabled",
        "mixed_authorities",
    ],
)
def test_invalid_authority_cannot_initialize(setup_engine: Engine, case: str) -> None:
    legacy, setup = _codecs()
    legacy_cookie = legacy.issue()[0]
    setup_cookie = setup.issue()[0]
    master_cookie = legacy.issue_master(_IDENTITY, 1)[0]
    values: dict[str, str] = {"setup_proof": _PROOF, "setup_cookie": setup_cookie}
    settings = _settings(setup_engine)
    if case == "empty_authority":
        values = {}
    elif case == "missing_proof":
        del values["setup_proof"]
    elif case == "missing_setup_cookie":
        del values["setup_cookie"]
    elif case == "wrong_proof":
        values["setup_proof"] = "synthetic incorrect first-setup authority 0002"
    elif case == "invalid_unicode_proof":
        values["setup_proof"] = "\ud800"
    elif case == "invalid_setup_cookie":
        values["setup_cookie"] = "invalid.synthetic.cookie"
    elif case == "legacy_cookie_as_setup":
        values["setup_cookie"] = legacy_cookie
    elif case == "master_cookie_as_setup":
        values["setup_cookie"] = master_cookie
    elif case == "setup_cookie_as_legacy":
        values = {"legacy_cookie": setup_cookie}
    elif case == "master_cookie_as_legacy":
        values = {"legacy_cookie": master_cookie}
    elif case == "legacy_disabled":
        settings = _settings(setup_engine, legacy=False)
        values = {"legacy_cookie": legacy_cookie}
    elif case == "proof_disabled":
        settings = _settings(setup_engine, proof=False)
    elif case == "mixed_authorities":
        values["legacy_cookie"] = legacy_cookie
    with pytest.raises(MasterSetupAuthorizationError) as error:
        _service(setup_engine, settings=settings).initialize(_TOKEN, _TOKEN, **values)
    assert _TOKEN not in repr(error.value)
    assert _PROOF not in repr(error.value)
    _empty(setup_engine)


@pytest.mark.parametrize("mode", ["legacy", "proof"])
def test_cookie_expiry_is_rechecked_inside_the_setup_write(setup_engine: Engine, mode: str) -> None:
    now = [1_000.0]
    legacy, setup = _codecs(lambda: now[0])
    cookie = legacy.issue()[0] if mode == "legacy" else setup.issue()[0]
    now[0] = 1_300.0
    values = (
        {"legacy_cookie": cookie}
        if mode == "legacy"
        else {"setup_proof": _PROOF, "setup_cookie": cookie}
    )
    with pytest.raises(MasterSetupAuthorizationError):
        _service(setup_engine, clock=lambda: now[0]).initialize(_TOKEN, _TOKEN, **values)
    _empty(setup_engine)


@pytest.mark.parametrize(
    ("token", "confirmation"),
    [
        (_TOKEN, _OTHER_TOKEN),
        ("short", "short"),
        ("x" * 1_025, "x" * 1_025),
        ("界" * 342, "界" * 342),
        ("x" * 32 + "\ud800", "x" * 32 + "\ud800"),
    ],
)
def test_invalid_token_or_confirmation_is_redacted_and_changes_nothing(
    setup_engine: Engine, token: str, confirmation: str
) -> None:
    cookie = _codecs()[1].issue()[0]
    with pytest.raises(ValueError) as error:
        _service(setup_engine).initialize(
            token, confirmation, setup_proof=_PROOF, setup_cookie=cookie
        )
    assert token not in str(error.value)
    assert confirmation not in str(error.value)
    _empty(setup_engine)


def test_unicode_token_byte_length_is_supported(setup_engine: Engine) -> None:
    token = "界" * 11
    cookie = _codecs()[1].issue()[0]
    state = _service(setup_engine).initialize(token, token, setup_proof=_PROOF, setup_cookie=cookie)
    with setup_engine.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(token) == state


def test_existing_master_rejects_all_setup_authority_without_changes(setup_engine: Engine) -> None:
    service = _service(setup_engine)
    setup_cookie = _codecs()[1].issue()[0]
    state = service.initialize(_TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=setup_cookie)
    with setup_engine.connect() as connection:
        old_identity = connection.execute(select(MasterIdentity)).all()
        old_audit = connection.execute(select(MasterAuditEvent)).all()
    for values in (
        {"setup_proof": _PROOF, "setup_cookie": setup_cookie},
        {"legacy_cookie": _codecs()[0].issue()[0]},
        {},
    ):
        with pytest.raises(MasterTokenAlreadyInitialized):
            service.initialize(_OTHER_TOKEN, _OTHER_TOKEN, **values)
    with setup_engine.connect() as connection:
        assert connection.execute(select(MasterIdentity)).all() == old_identity
        assert connection.execute(select(MasterAuditEvent)).all() == old_audit
        assert MasterTokenRepository(connection).authenticate(_TOKEN) == state


def test_audit_failure_rolls_back_the_new_identity(
    setup_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(MasterAuditRepository, "add_success", reject_audit)
    cookie = _codecs()[1].issue()[0]
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        _service(setup_engine).initialize(_TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie)
    _empty(setup_engine)


def test_noncurrent_schema_is_rejected_without_initialization(setup_engine: Engine) -> None:
    with immediate_transaction(setup_engine) as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '20260929_0016'"))
    cookie = _codecs()[1].issue()[0]
    with pytest.raises(MasterSetupUnavailableError):
        _service(setup_engine).initialize(_TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie)
    _empty(setup_engine)
    with setup_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == ("20260929_0016")


def test_unmigrated_database_does_not_create_tables(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{(tmp_path / 'unmigrated.sqlite').as_posix()}")
    try:
        cookie = _codecs()[1].issue()[0]
        with pytest.raises(MasterSetupUnavailableError):
            _service(engine).initialize(_TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT name FROM sqlite_schema").all() == []
    finally:
        engine.dispose()


def test_disabled_admin_configuration_cannot_be_bypassed_by_injected_codecs(
    setup_engine: Engine,
) -> None:
    settings = Settings(  # type: ignore[call-arg]  # BaseSettings supports _env_file at runtime.
        _env_file=None,
        environment="test",
        database_url=str(setup_engine.url),
        admin_password_hash=None,
        admin_setup_token=None,
        admin_session_signing_secret=None,
    )
    cookie = _codecs()[1].issue()[0]
    with pytest.raises(MasterSetupUnavailableError):
        _service(setup_engine, settings=settings).initialize(
            _TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie
        )
    _empty(setup_engine)


def test_concurrent_first_setup_creates_exactly_one_identity_and_audit(
    setup_engine: Engine,
) -> None:
    barrier = Barrier(2)
    cookie = _codecs()[1].issue()[0]

    def initialize(identity_id: str) -> MasterTokenState | MasterTokenAlreadyInitialized:
        service = _service(setup_engine, identity_id=identity_id)
        barrier.wait(timeout=5)
        try:
            return service.initialize(_TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie)
        except MasterTokenAlreadyInitialized as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, ("1" * 32, "2" * 32)))
    winners = [result for result in results if isinstance(result, MasterTokenState)]
    losers = [result for result in results if isinstance(result, MasterTokenAlreadyInitialized)]
    assert len(winners) == len(losers) == 1
    with setup_engine.connect() as connection:
        assert MasterTokenRepository(connection).authenticate(_TOKEN) == winners[0]
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 1
        assert connection.scalar(select(func.count()).select_from(MasterAuditEvent)) == 1


def test_browser_setup_races_local_cli_without_overwriting_the_winner(setup_engine: Engine) -> None:
    barrier = Barrier(2)
    cookie = _codecs()[1].issue()[0]

    def browser() -> tuple[str, MasterTokenState | MasterTokenAlreadyInitialized]:
        service = _service(setup_engine, identity_id="1" * 32)
        barrier.wait(timeout=5)
        try:
            return "web", service.initialize(
                _TOKEN, _TOKEN, setup_proof=_PROOF, setup_cookie=cookie
            )
        except MasterTokenAlreadyInitialized as error:
            return "web", error

    def local_cli() -> tuple[str, MasterTokenState | MasterTokenAlreadyInitialized]:
        barrier.wait(timeout=5)
        try:
            with immediate_transaction(setup_engine) as connection:
                state = MasterTokenRepository(
                    connection, identity_factory=lambda: "2" * 32
                ).initialize_from_local_cli(_OTHER_TOKEN, now=1_000_000_000)
            return "cli", state
        except MasterTokenAlreadyInitialized as error:
            return "cli", error

    with ThreadPoolExecutor(max_workers=2) as pool:
        web_future = pool.submit(browser)
        cli_future = pool.submit(local_cli)
        results = [web_future.result(timeout=10), cli_future.result(timeout=10)]
    winners = [(mode, state) for mode, state in results if isinstance(state, MasterTokenState)]
    losers = [result for _, result in results if isinstance(result, MasterTokenAlreadyInitialized)]
    assert len(winners) == len(losers) == 1
    mode, state = winners[0]
    winning_token, losing_token = (
        (_TOKEN, _OTHER_TOKEN) if mode == "web" else (_OTHER_TOKEN, _TOKEN)
    )
    with setup_engine.connect() as connection:
        repository = MasterTokenRepository(connection)
        assert repository.authenticate(winning_token) == state
        assert repository.authenticate(losing_token) is None
        assert connection.scalar(select(func.count()).select_from(MasterIdentity)) == 1
        audits = connection.execute(select(MasterAuditEvent.__table__)).mappings().all()
        assert len(audits) == (1 if mode == "web" else 0)
        if mode == "web":
            assert audits[0]["identity_id"] == state.identity_id
            assert audits[0]["action"] == "auth.master.initialize"
