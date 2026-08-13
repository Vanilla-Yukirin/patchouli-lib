"""Run a synthetic Docker backup/restore drill against fresh named volumes.

This is experimental evidence for the public recovery proposal. It does not
select a deployment target, touch an existing database, or authorize cutover.
Host-side diagnostics are deliberately fixed and redact subprocess output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Protocol, cast

EXIT_FAILED: Final = 1
EXIT_INCONCLUSIVE: Final = 2
DEFAULT_TIMEOUT_SECONDS: Final = 300.0
CONTAINER_TIMEOUT_SECONDS: Final = 120.0
CLEANUP_TIMEOUT_SECONDS: Final = 30.0
RUNTIME_UID: Final = 10001
RUNTIME_GID: Final = 10001
APP_VERSION: Final = "0.1.0a0"
DATABASE_PATH: Final = Path("/data/patchouli.db")
BACKUP_ROOT: Final = Path("/backups")
BUNDLE_PATH: Final = BACKUP_ROOT / "bundle"
EXPECTED_PATH: Final = BACKUP_ROOT / "expected.json"
SOURCE_MARKER: Final = Path("/data/source-volume-only")
DESTINATION_MARKER: Final = Path("/data/restored-destination-only")
_EXPECTED_LIMIT: Final = 64 * 1024
_SAFE_RUN_ID: Final = re.compile(r"\A[a-z0-9][a-z0-9-]{7,47}\Z")
_PROXY_NAMES: Final = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_DOCKER_REDIRECT_NAMES: Final = frozenset(
    {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
    }
)
_LOCAL_DOCKER_ENDPOINTS: Final = frozenset(
    {
        "npipe:////./pipe/docker_engine",
        "unix:///var/run/docker.sock",
    }
)
_TABLES: Final = (
    "libraries",
    "sections",
    "books",
    "pages",
    "revisions",
    "page_revision_append_guards",
    "page_sources",
    "page_identifier_registry",
    "page_id_collision_counters",
    "auth_callers",
    "auth_credentials",
    "auth_section_grants",
    "auth_audit_events",
    "operator_bootstrap_markers",
    "idempotency_records",
)


class SmokeFailure(RuntimeError):
    """The drill ran and failed a required check."""


class SmokeInconclusive(RuntimeError):
    """The local platform could not run the drill."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        timeout_seconds: float,
        environment: Mapping[str, str],
    ) -> CommandResult: ...


def _diagnostic_phase(arguments: Sequence[str]) -> str:
    safe_words = {
        "build": "image build",
        "create": "resource create",
        "inspect": "resource inspect",
        "rm": "resource cleanup",
        "start": "container phase",
        "version": "engine probe",
    }
    for argument in arguments:
        if argument in safe_words:
            return safe_words[argument]
    return "container operation"


def _subprocess_environment(
    source: Mapping[str, str] | None = None,
    *,
    docker_config: Path | None = None,
) -> dict[str, str]:
    """Return a minimal host environment without proxy or Docker redirection knobs."""

    values = os.environ if source is None else source
    allowed = {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
    }
    environment = {
        key: value
        for key, value in values.items()
        if key.upper() in allowed
        and key not in _PROXY_NAMES
        and key.upper() not in _DOCKER_REDIRECT_NAMES
    }
    if docker_config is not None:
        environment["DOCKER_CONFIG"] = str(docker_config)
    return environment


def _subprocess_runner(
    arguments: Sequence[str],
    timeout_seconds: float,
    environment: Mapping[str, str],
) -> CommandResult:
    try:
        completed = subprocess.run(
            tuple(arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeFailure(
            f"Container {_diagnostic_phase(arguments)} could not complete."
        ) from None
    if len(completed.stdout) > 4_096:
        raise SmokeFailure("Container command returned excessive output.")
    return CommandResult(completed.returncode, completed.stdout)


@dataclass(slots=True)
class DockerClient:
    executable: str
    timeout_seconds: float
    runner: CommandRunner = _subprocess_runner
    environment: Mapping[str, str] = field(default_factory=_subprocess_environment)

    def invoke(self, *arguments: str, timeout_seconds: float | None = None) -> CommandResult:
        return self.runner(
            (self.executable, "--context", "default", *arguments),
            self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            self.environment,
        )

    def require(self, *arguments: str, timeout_seconds: float | None = None) -> None:
        if self.invoke(*arguments, timeout_seconds=timeout_seconds).returncode != 0:
            raise SmokeFailure(f"Container {_diagnostic_phase(arguments)} failed.")

    def require_absent(self, resource: str, name: str) -> None:
        result = self.invoke(resource, "inspect", name).returncode
        if result == 0:
            raise SmokeFailure("Synthetic container resource name collided.")
        if result != 1:
            raise SmokeFailure("Container resource state could not be established.")

    def require_local_default_endpoint(self) -> None:
        try:
            result = self.invoke(
                "context",
                "inspect",
                "default",
                "--format",
                '{{(index .Endpoints "docker").Host}}',
            )
        except SmokeFailure:
            raise SmokeInconclusive("Docker default context is unavailable.") from None
        if result.returncode != 0:
            raise SmokeInconclusive("Docker default context is unavailable.")
        try:
            endpoint = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            raise SmokeFailure("Docker default endpoint is invalid.") from None
        if endpoint not in _LOCAL_DOCKER_ENDPOINTS:
            raise SmokeFailure("Docker default endpoint is not local.")

    def owned_resource(self, kind: str, name: str, run_id: str) -> OwnedResource:
        resource = self.find_owned_resource(kind, name, run_id)
        if resource is None:
            raise SmokeFailure("Synthetic resource ownership could not be verified.")
        return resource

    def find_owned_resource(
        self,
        kind: str,
        name: str,
        run_id: str,
    ) -> OwnedResource | None:
        label = "org.patchouli.smoke"
        if kind in {"container", "image"}:
            template = f'{{{{.Id}}}}|{{{{index .Config.Labels "{label}"}}}}'
        elif kind == "volume":
            template = f'{{{{.Name}}}}|{{{{index .Labels "{label}"}}}}'
        else:
            raise SmokeFailure("Synthetic resource kind is invalid.")
        result = self.invoke(kind, "inspect", "--format", template, name)
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise SmokeFailure("Synthetic resource ownership could not be verified.")
        try:
            identity, observed_label = (
                result.stdout.decode("ascii", errors="strict").strip().split("|", maxsplit=1)
            )
        except (UnicodeDecodeError, ValueError):
            raise SmokeFailure("Synthetic resource ownership could not be verified.") from None
        if not identity or observed_label != run_id:
            raise SmokeFailure("Synthetic resource ownership could not be verified.")
        return OwnedResource(kind=kind, identity=identity, run_id=run_id)


@dataclass(frozen=True, slots=True)
class OwnedResource:
    kind: str
    identity: str
    run_id: str


@dataclass(slots=True)
class ResourceLedger:
    docker: DockerClient
    containers: list[OwnedResource] = field(default_factory=list)
    volumes: list[OwnedResource] = field(default_factory=list)
    images: list[OwnedResource] = field(default_factory=list)

    def remove_container(self, resource: OwnedResource) -> None:
        self._require_owned(resource)
        self.docker.require("container", "rm", "--force", resource.identity)
        self.containers.remove(resource)

    def _require_owned(self, resource: OwnedResource) -> None:
        observed = self.docker.owned_resource(
            resource.kind,
            resource.identity,
            resource.run_id,
        )
        if observed != resource:
            raise SmokeFailure("Synthetic resource ownership changed.")

    def cleanup(self) -> None:
        failed = False
        for resource in reversed(self.containers):
            if not self._remove(resource, "container", "rm", "--force", resource.identity):
                failed = True
        self.containers.clear()
        for resource in reversed(self.volumes):
            if not self._remove(resource, "volume", "rm", resource.identity):
                failed = True
        self.volumes.clear()
        for resource in reversed(self.images):
            if not self._remove(resource, "image", "rm", "--force", resource.identity):
                failed = True
        self.images.clear()
        if failed:
            raise SmokeFailure("Synthetic container cleanup failed.")

    def _remove(self, resource: OwnedResource, *arguments: str) -> bool:
        try:
            self._require_owned(resource)
            return (
                self.docker.invoke(
                    *arguments,
                    timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
                ).returncode
                == 0
            )
        except (SmokeFailure, OSError, TimeoutError, subprocess.TimeoutExpired):
            return False


def _new_run_id() -> str:
    return f"patchouli-smoke-{os.getpid():x}-{secrets.token_hex(6)}"


def _validate_host_inputs(repository_root: Path, run_id: str) -> tuple[Path, Path]:
    try:
        root = repository_root.resolve(strict=True)
        script = Path(__file__).resolve(strict=True)
    except (OSError, RuntimeError):
        raise SmokeFailure("Smoke inputs are not available.") from None
    if not _SAFE_RUN_ID.fullmatch(run_id):
        raise SmokeFailure("Synthetic run identifier is invalid.")
    if not (root / "Dockerfile").is_file() or script.parent.parent != root:
        raise SmokeFailure("Smoke inputs do not match the repository root.")
    if "," in str(script):
        raise SmokeInconclusive("Local path cannot be represented as a Docker mount.")
    return root, script


def _container_environment() -> tuple[str, ...]:
    values: list[str] = []
    for name in sorted(_PROXY_NAMES):
        values.extend(("--env", f"{name}="))
    values.extend(("--env", "PYTHONDONTWRITEBYTECODE=1"))
    return tuple(values)


def _common_container_arguments(script: Path) -> tuple[str, ...]:
    return (
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        *_container_environment(),
        "--mount",
        f"type=bind,source={script},target=/app/container_restore_smoke.py,readonly",
    )


def _create_volume(
    docker: DockerClient,
    ledger: ResourceLedger,
    name: str,
    run_id: str,
) -> OwnedResource:
    docker.require_absent("volume", name)
    return _create_owned_resource(
        docker,
        ledger.volumes,
        kind="volume",
        name=name,
        run_id=run_id,
        arguments=("volume", "create", "--label", f"org.patchouli.smoke={run_id}", name),
    )


def _create_owned_resource(
    docker: DockerClient,
    collection: list[OwnedResource],
    *,
    kind: str,
    name: str,
    run_id: str,
    arguments: Sequence[str],
) -> OwnedResource:
    try:
        docker.require(*arguments)
    except BaseException:
        resource = _reconcile_ambiguous_creation(docker, kind, name, run_id)
        collection.append(resource)
        raise
    try:
        observed = docker.find_owned_resource(kind, name, run_id)
    except (SmokeFailure, OSError, TimeoutError, subprocess.TimeoutExpired):
        raise SmokeFailure("Synthetic resource cleanup could not be verified.") from None
    if observed is None:
        raise SmokeFailure("Synthetic resource cleanup could not be verified.")
    collection.append(observed)
    return observed


def _reconcile_ambiguous_creation(
    docker: DockerClient,
    kind: str,
    name: str,
    run_id: str,
) -> OwnedResource:
    try:
        observed = docker.find_owned_resource(kind, name, run_id)
    except (SmokeFailure, OSError, TimeoutError, subprocess.TimeoutExpired):
        raise SmokeFailure("Synthetic resource cleanup could not be verified.") from None
    if observed is None:
        raise SmokeFailure("Synthetic resource cleanup could not be verified.")
    return observed


def _create_container(
    docker: DockerClient,
    ledger: ResourceLedger,
    *,
    run_id: str,
    name: str,
    arguments: Sequence[str],
) -> OwnedResource:
    docker.require_absent("container", name)
    return _create_owned_resource(
        docker,
        ledger.containers,
        kind="container",
        name=name,
        run_id=run_id,
        arguments=(
            "container",
            "create",
            "--label",
            f"org.patchouli.smoke={run_id}",
            "--name",
            name,
            *arguments,
        ),
    )


def _run_container(
    docker: DockerClient,
    ledger: ResourceLedger,
    resource: OwnedResource,
) -> None:
    ledger._require_owned(resource)
    docker.require(
        "container",
        "start",
        "--attach",
        resource.identity,
        timeout_seconds=CONTAINER_TIMEOUT_SECONDS,
    )
    ledger.remove_container(resource)


def run_docker_smoke(
    repository_root: Path,
    *,
    docker: DockerClient,
    run_id: str | None = None,
) -> None:
    """Build the current image and prove restore into a distinct fresh volume."""

    selected_run = _new_run_id() if run_id is None else run_id
    root, script = _validate_host_inputs(repository_root, selected_run)
    temporary_config: TemporaryDirectory[str] | None = None
    if docker.runner is _subprocess_runner:
        temporary_config = TemporaryDirectory(prefix="patchouli-docker-config-")
        docker.environment = _subprocess_environment(
            docker_config=Path(temporary_config.name),
        )

    image = f"{selected_run}:local"
    source_volume = f"{selected_run}-source"
    backup_volume = f"{selected_run}-backup"
    destination_volume = f"{selected_run}-destination"
    source_container = f"{selected_run}-source"
    restore_container = f"{selected_run}-restore"
    verify_container = f"{selected_run}-verify"
    ledger = ResourceLedger(docker)
    primary_error: BaseException | None = None
    try:
        docker.require_local_default_endpoint()
        try:
            docker_available = docker.invoke("version").returncode == 0
        except SmokeFailure:
            docker_available = False
        if not docker_available:
            raise SmokeInconclusive("Docker engine is unavailable.")
        docker.require_absent("image", image)
        _create_owned_resource(
            docker,
            ledger.images,
            kind="image",
            name=image,
            run_id=selected_run,
            arguments=(
                "build",
                "--quiet",
                "--pull=false",
                "--label",
                f"org.patchouli.smoke={selected_run}",
                "--tag",
                image,
                str(root),
            ),
        )
        _create_volume(docker, ledger, source_volume, selected_run)
        _create_volume(docker, ledger, backup_volume, selected_run)

        common = _common_container_arguments(script)
        source_resource = _create_container(
            docker,
            ledger,
            run_id=selected_run,
            name=source_container,
            arguments=(
                *common,
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={source_volume},target=/data",
                "--mount",
                f"type=volume,source={backup_volume},target=/backups",
                image,
                "/app/container_restore_smoke.py",
                "--internal-phase",
                "seed-backup",
            ),
        )
        _run_container(docker, ledger, source_resource)

        # The destination is created only after the source writer has stopped
        # and been removed. It is never mounted together with the source volume.
        _create_volume(docker, ledger, destination_volume, selected_run)
        restore_resource = _create_container(
            docker,
            ledger,
            run_id=selected_run,
            name=restore_container,
            arguments=(
                *common,
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={destination_volume},target=/data",
                "--mount",
                f"type=volume,source={backup_volume},target=/backups,readonly",
                image,
                "/app/container_restore_smoke.py",
                "--internal-phase",
                "restore",
            ),
        )
        _run_container(docker, ledger, restore_resource)

        verify_resource = _create_container(
            docker,
            ledger,
            run_id=selected_run,
            name=verify_container,
            arguments=(
                *common,
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={destination_volume},target=/data",
                "--mount",
                f"type=volume,source={backup_volume},target=/backups,readonly",
                image,
                "/app/container_restore_smoke.py",
                "--internal-phase",
                "verify",
            ),
        )
        _run_container(docker, ledger, verify_resource)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            ledger.cleanup()
        except SmokeFailure as cleanup_error:
            if primary_error is None:
                raise
            raise SmokeFailure("Smoke failed and synthetic cleanup also failed.") from (
                cleanup_error
            )
        finally:
            if temporary_config is not None:
                temporary_config.cleanup()


def _id_factory(start: int = 1) -> Callable[[], str]:
    current = start

    def create() -> str:
        nonlocal current
        value = f"{current:032x}"
        current += 1
        return value

    return create


def _json_value(value: object) -> object:
    if type(value) is bytes:
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if value is None or type(value) in {int, str}:
        return value
    raise SmokeFailure("Stored state contained an unsupported value.")


def _state_fingerprint(connection: sqlite3.Connection) -> str:
    state: dict[str, object] = {}
    for table in _TABLES:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        names = [row[1] for row in columns]
        if not names or not all(isinstance(name, str) for name in names):
            raise SmokeFailure("Stored state schema is incomplete.")
        order = ", ".join(f'"{name}"' for name in names)
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall()
        state[table] = [[_json_value(value) for value in row] for row in rows]
    encoded = json.dumps(state, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _semantic_state(connection: sqlite3.Connection) -> dict[str, object]:
    pages = connection.execute(
        "SELECT page_id, section_id, book_id, hex(page_uid), title, "
        "current_revision_id, current_revision_number "
        "FROM pages ORDER BY page_id"
    ).fetchall()
    revisions = connection.execute(
        "SELECT revision_id, hex(page_uid), revision_number, hex(content_sha256), content_md "
        "FROM revisions ORDER BY revision_number"
    ).fetchall()
    sources = connection.execute(
        "SELECT source_id, hex(page_uid), revision_id, revision_number, kind, "
        "locator, captured_at "
        "FROM page_sources ORDER BY revision_number"
    ).fetchall()
    audit_actions = connection.execute(
        "SELECT action FROM auth_audit_events ORDER BY occurred_at, id"
    ).fetchall()
    bodies = connection.execute(
        "SELECT response_body FROM idempotency_records ORDER BY route_template"
    ).fetchall()
    auth_relationships = connection.execute(
        "SELECT c.kind, count(DISTINCT cr.id), g.section_id, g.action "
        "FROM auth_callers AS c "
        "LEFT JOIN auth_credentials AS cr "
        "ON cr.library_id = c.library_id AND cr.caller_id = c.id "
        "LEFT JOIN auth_section_grants AS g "
        "ON g.library_id = c.library_id AND g.caller_id = c.id "
        "GROUP BY c.library_id, c.id, c.kind, g.section_id, g.action ORDER BY c.kind"
    ).fetchall()
    idempotency_relationships = connection.execute(
        "SELECT c.kind, r.method, r.route_template, r.original_request_id "
        "FROM idempotency_records AS r "
        "JOIN auth_callers AS c "
        "ON c.library_id = r.library_id AND c.id = r.caller_id "
        "ORDER BY r.route_template"
    ).fetchall()
    citations: list[object] = []
    for (body,) in bodies:
        if type(body) is not bytes:
            raise SmokeFailure("Stored replay body is invalid.")
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("citation"), dict):
            raise SmokeFailure("Stored replay citation is invalid.")
        citations.append(parsed["citation"])
    state: dict[str, object] = {
        "counts": {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in _TABLES
        },
        "pages": [
            [page_id, section_id, book_id, page_uid.lower(), title, revision_id, number]
            for page_id, section_id, book_id, page_uid, title, revision_id, number in pages
        ],
        "revisions": [
            [revision_id, page_uid.lower(), number, digest.lower(), content.decode("utf-8")]
            for revision_id, page_uid, number, digest, content in revisions
        ],
        "sources": [
            [source_id, page_uid.lower(), revision_id, number, kind, locator, captured_at]
            for source_id, page_uid, revision_id, number, kind, locator, captured_at in sources
        ],
        "audit_actions": [row[0] for row in audit_actions],
        "auth_relationships": [list(row) for row in auth_relationships],
        "idempotency_relationships": [list(row) for row in idempotency_relationships],
        "citations": citations,
        "state_fingerprint": _state_fingerprint(connection),
    }
    _require_minimum_semantics(state)
    return state


def _require_minimum_semantics(state: Mapping[str, object]) -> None:
    counts = state.get("counts")
    pages = state.get("pages")
    revisions = state.get("revisions")
    sources = state.get("sources")
    audits = state.get("audit_actions")
    auth_relationships = state.get("auth_relationships")
    idempotency_relationships = state.get("idempotency_relationships")
    citations = state.get("citations")
    if (
        not isinstance(counts, dict)
        or not isinstance(pages, list)
        or not isinstance(revisions, list)
        or not isinstance(sources, list)
        or not isinstance(audits, list)
        or not isinstance(auth_relationships, list)
        or not isinstance(idempotency_relationships, list)
        or not isinstance(citations, list)
    ):
        raise SmokeFailure("Semantic state is incomplete.")
    required_counts = {
        "libraries": 1,
        "sections": 1,
        "books": 1,
        "pages": 1,
        "revisions": 2,
        "page_revision_append_guards": 0,
        "page_sources": 2,
        "page_identifier_registry": 1,
        "page_id_collision_counters": 1,
        "auth_callers": 2,
        "auth_credentials": 2,
        "auth_section_grants": 1,
        "auth_audit_events": 6,
        "operator_bootstrap_markers": 1,
        "idempotency_records": 2,
    }
    if any(counts.get(name) != minimum for name, minimum in required_counts.items()):
        raise SmokeFailure("Semantic state is incomplete.")
    if (
        len(pages) != 1
        or not isinstance(pages[0], list)
        or len(pages[0]) != 7
        or pages[0][4] != "Synthetic Restore Archive"
        or pages[0][6] != 2
    ):
        raise SmokeFailure("Semantic Page state is incomplete.")
    if (
        len(revisions) != 2
        or not all(isinstance(row, list) and len(row) == 5 for row in revisions)
        or [row[2] for row in revisions] != [1, 2]
        or any(row[1] != pages[0][3] for row in revisions)
        or pages[0][5] != revisions[1][0]
        or [row[4] for row in revisions]
        != [
            "# Synthetic restore\n\nRevision one.\n",
            "# Synthetic restore\n\nRevision two.\n",
        ]
        or any(row[3] != hashlib.sha256(row[4].encode("utf-8")).hexdigest() for row in revisions)
        or len(sources) != 2
        or not all(isinstance(row, list) and len(row) == 7 for row in sources)
        or [row[3] for row in sources] != [1, 2]
        or any(row[1] != pages[0][3] for row in sources)
        or [(row[2], row[3]) for row in sources] != [(row[0], row[2]) for row in revisions]
        or [row[4] for row in sources] != ["synthetic", "synthetic"]
        or [row[5] for row in sources]
        != [
            "urn:patchouli:synthetic:revision:1",
            "urn:patchouli:synthetic:revision:2",
        ]
    ):
        raise SmokeFailure("Semantic Revision or Source state is incomplete.")
    required_audits = {
        "operator.bootstrap",
        "auth.caller.create",
        "auth.credential.create",
        "auth.grant.add",
        "content.archive.create",
        "content.archive.revise",
    }
    if set(audits) != required_audits or len(audits) != 6:
        raise SmokeFailure("Semantic audit state is incomplete.")
    if auth_relationships != [
        ["agent", 1, pages[0][1], "archive:write"],
        ["operator", 1, None, None],
    ]:
        raise SmokeFailure("Semantic authentication relationships are incomplete.")
    if idempotency_relationships != [
        [
            "agent",
            "POST",
            "/api/v1/sections/{section_id}/books/{book_id}/pages",
            "req_" + "1" * 32,
        ],
        [
            "agent",
            "POST",
            "/api/v1/sections/{section_id}/pages/{page_id}/revisions",
            "req_" + "2" * 32,
        ],
    ]:
        raise SmokeFailure("Semantic idempotency relationships are incomplete.")
    if len(citations) != 2:
        raise SmokeFailure("Semantic citation state is incomplete.")
    page_id = pages[0][0]
    expected_revisions = {(row[0], row[2]) for row in revisions}
    observed_citations = {
        (
            citation.get("revision_id"),
            citation.get("revision_number"),
            citation.get("section_id"),
            citation.get("href"),
        )
        for citation in citations
        if isinstance(citation, dict)
        and citation.get("page_id") == page_id
        and isinstance(citation.get("href"), str)
    }
    expected_citations = {
        (
            revision_id,
            revision_number,
            pages[0][1],
            f"/api/v1/sections/{pages[0][1]}/pages/{page_id}/revisions/{revision_number}",
        )
        for revision_id, revision_number in expected_revisions
    }
    if observed_citations != expected_citations:
        raise SmokeFailure("Semantic citations do not bind exact Revisions.")


def _require_directory_names(path: Path, expected: frozenset[str]) -> None:
    try:
        observed = frozenset(entry.name for entry in path.iterdir())
    except (OSError, RecursionError):
        raise SmokeFailure("Runtime volume contents could not be verified.") from None
    if observed != expected:
        raise SmokeFailure("Runtime volume contents are not exact.")


def _runtime_identity() -> None:
    effective_user_id = cast(Callable[[], int] | None, getattr(os, "geteuid", None))
    effective_group_id = cast(Callable[[], int] | None, getattr(os, "getegid", None))
    if effective_user_id is None or effective_group_id is None:
        raise SmokeFailure("Container runtime does not expose numeric identity.")
    if effective_user_id() != RUNTIME_UID or effective_group_id() != RUNTIME_GID:
        raise SmokeFailure("Container runtime identity is not deterministic.")
    for directory in (DATABASE_PATH.parent, BACKUP_ROOT):
        metadata = directory.stat()
        if metadata.st_uid != RUNTIME_UID or metadata.st_gid != RUNTIME_GID:
            raise SmokeFailure("Container runtime directory ownership is invalid.")


def _seed_backup() -> None:
    from sqlalchemy import Connection, text

    from patchouli_lib.auth import OperatorBootstrap, SectionAction
    from patchouli_lib.auth.repository import AuthRepository
    from patchouli_lib.backup import (
        BackupArtifactIdentity,
        create_backup,
        verify_backup_bundle,
    )
    from patchouli_lib.content import (
        AppendArchiveRevisionCommand,
        ArchiveIdempotencyKey,
        ArchiveMutationReplay,
        ArchiveMutationSuccess,
        ArchiveSourceInput,
        CreateArchiveCommand,
    )
    from patchouli_lib.content.service import ArchiveService
    from patchouli_lib.database import build_engine, immediate_transaction
    from patchouli_lib.identifiers import parse_occurrence_time
    from patchouli_lib.library.repository import LibraryRepository
    from patchouli_lib.library.schemas import LibraryStructureSeed
    from patchouli_lib.library.service import LibrarySeedService
    from patchouli_lib.operator.service import OperatorBootstrapService, OperatorService

    _runtime_identity()
    _require_directory_names(DATABASE_PATH.parent, frozenset())
    _require_directory_names(BACKUP_ROOT, frozenset())
    SOURCE_MARKER.write_text("source-volume-only\n", encoding="ascii")

    migration_environment = os.environ.copy()
    migration_environment["PATCHOULI_DATABASE_URL"] = f"sqlite:///{DATABASE_PATH.as_posix()}"
    migration_environment["PATCHOULI_ENVIRONMENT"] = "test"
    try:
        subprocess.run(
            ("alembic", "upgrade", "head"),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CONTAINER_TIMEOUT_SECONDS,
            env=migration_environment,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise SmokeFailure("Synthetic migration failed.") from None

    engine = build_engine(f"sqlite:///{DATABASE_PATH.as_posix()}")
    identifiers = _id_factory()
    operation_times = iter(range(1_900_000_000_000_000, 1_900_000_000_000_100))

    def clock() -> int:
        return next(operation_times)

    try:
        with engine.connect() as connection:
            if connection.execute(text("PRAGMA journal_mode = WAL")).scalar_one().lower() != "wal":
                raise SmokeFailure("WAL journal mode is unavailable.")

        with immediate_transaction(engine) as connection:
            structure = LibrarySeedService(
                LibraryRepository(connection), id_factory=identifiers, clock=clock
            ).seed(
                LibraryStructureSeed(
                    library_name="Synthetic Restore Library",
                    section_name="Synthetic Restore Section",
                    book_name="Synthetic Restore Book",
                )
            )
        with immediate_transaction(engine) as connection:
            operator = OperatorBootstrapService(
                AuthRepository(connection), id_factory=identifiers, clock=clock
            ).bootstrap(
                OperatorBootstrap(
                    library_id=structure.library.id,
                    operator_name="Synthetic Operator",
                    credential_expires_at=2_000_000_000_000_000,
                    request_id="req_smoke_bootstrap",
                )
            )
        operator_token = operator.credential.value
        with immediate_transaction(engine) as connection:
            service = OperatorService(
                AuthRepository(connection),
                id_factory=identifiers,
                clock=clock,
            )
            agent = service.create_agent_caller(
                operator_token,
                library_id=structure.library.id,
                name="Synthetic Agent",
                request_id="req_smoke_agent",
            )
        with immediate_transaction(engine) as connection:
            issued = OperatorService(
                AuthRepository(connection), id_factory=identifiers, clock=clock
            ).create_credential(
                operator_token,
                library_id=structure.library.id,
                caller_id=agent.id,
                expires_at=2_000_000_000_000_000,
                request_id="req_smoke_credential",
            )
        with immediate_transaction(engine) as connection:
            OperatorService(
                AuthRepository(connection), id_factory=identifiers, clock=clock
            ).add_grant(
                operator_token,
                library_id=structure.library.id,
                caller_id=agent.id,
                section_id=structure.section.id,
                action=SectionAction.ARCHIVE_WRITE,
                request_id="req_smoke_grant",
            )

        occurrence = parse_occurrence_time("2026-08-13T10:00:00.123456Z")
        revision_ids = iter(("rev_" + "1" * 32, "rev_" + "2" * 32))
        archive_clock = iter((1_900_000_000_000_010, 1_900_000_000_000_020))

        def archive(connection: Connection) -> ArchiveService:
            return ArchiveService(
                connection,
                clock=lambda: next(archive_clock),
                id_factory=identifiers,
                page_uid_factory=lambda: b"P" * 16,
                revision_id_factory=lambda: next(revision_ids),
            )

        create_command = CreateArchiveCommand(
            library_id=structure.library.id,
            section_id=structure.section.id,
            book_id=structure.book.id,
            title="Synthetic Restore Archive",
            occurred_at=occurrence.utc_microseconds,
            content_md=b"# Synthetic restore\n\nRevision one.\n",
            source=ArchiveSourceInput(
                kind="synthetic",
                locator="urn:patchouli:synthetic:revision:1",
                captured_at=occurrence.utc_microseconds,
            ),
            request_id="req_" + "1" * 32,
        )
        create_key = ArchiveIdempotencyKey(key_digest=hashlib.sha256(b"create-smoke").digest())
        with immediate_transaction(engine) as connection:
            created = archive(connection).create_archive(issued.value, create_command, create_key)
        if not isinstance(created, ArchiveMutationSuccess):
            raise SmokeFailure("Archive create did not produce fresh state.")
        with closing(sqlite3.connect(DATABASE_PATH)) as replay_connection:
            before_replay = _state_fingerprint(replay_connection)
        with immediate_transaction(engine) as connection:
            replay = ArchiveService(
                connection,
                clock=lambda: 1_900_000_000_000_011,
            ).create_archive(
                issued.value,
                create_command,
                create_key,
            )
        if (
            not isinstance(replay, ArchiveMutationReplay)
            or replay.response.response_body != created.response.response_body
            or replay.response.original_request_id != created.response.original_request_id
            or replay.response.response_location != created.response.response_location
            or replay.response.response_etag != created.response.response_etag
        ):
            raise SmokeFailure("Archive create replay did not preserve the original response.")
        with closing(sqlite3.connect(DATABASE_PATH)) as replay_connection:
            after_replay = _state_fingerprint(replay_connection)
            replay_counts = {
                table: replay_connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in (
                    "pages",
                    "revisions",
                    "page_sources",
                    "auth_audit_events",
                    "idempotency_records",
                )
            }
        if after_replay != before_replay or replay_counts != {
            "pages": 1,
            "revisions": 1,
            "page_sources": 1,
            "auth_audit_events": 5,
            "idempotency_records": 1,
        }:
            raise SmokeFailure("Archive replay changed persisted mutation state.")
        with immediate_transaction(engine) as connection:
            revised = archive(connection).append_revision(
                issued.value,
                AppendArchiveRevisionCommand(
                    library_id=structure.library.id,
                    section_id=structure.section.id,
                    page_id=created.page.page_id,
                    expected_etag=created.response.response_etag,
                    content_md=b"# Synthetic restore\n\nRevision two.\n",
                    source=ArchiveSourceInput(
                        kind="synthetic",
                        locator="urn:patchouli:synthetic:revision:2",
                        captured_at=occurrence.utc_microseconds + 1,
                    ),
                    request_id="req_" + "2" * 32,
                ),
                ArchiveIdempotencyKey(key_digest=hashlib.sha256(b"revise-smoke").digest()),
            )
        if not isinstance(revised, ArchiveMutationSuccess):
            raise SmokeFailure("Archive revise did not produce fresh state.")

        with closing(sqlite3.connect(DATABASE_PATH)) as sqlite_connection:
            expected = _semantic_state(sqlite_connection)
        wal_path = Path(f"{DATABASE_PATH}-wal")
        if not wal_path.is_file() or wal_path.stat().st_size == 0:
            raise SmokeFailure("Synthetic source did not retain a WAL sidecar.")
        result = create_backup(
            engine,
            BUNDLE_PATH,
            artifact_identity=BackupArtifactIdentity(
                identity="container-restore-smoke",
                digest=("sha256:" + hashlib.sha256(b"container-restore-smoke/v1").hexdigest()),
            ),
            app_version=APP_VERSION,
        )
        if result.manifest.source_journal_mode != "wal":
            raise SmokeFailure("Backup did not record the WAL source mode.")
        if verify_backup_bundle(BUNDLE_PATH, app_version=APP_VERSION) != result.manifest:
            raise SmokeFailure("Backup verification did not bind the manifest.")
        expected["backup_sha256"] = result.manifest.sha256
        payload = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(EXPECTED_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise SmokeFailure("Expected-state artifact could not be written.")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        engine.dispose()


def _load_expected() -> dict[str, object]:
    try:
        with EXPECTED_PATH.open("rb") as source:
            data = source.read(_EXPECTED_LIMIT + 1)
        if not 1 <= len(data) <= _EXPECTED_LIMIT:
            raise SmokeFailure("Expected-state artifact is invalid.")
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeFailure("Expected-state artifact is invalid.") from None
    if not isinstance(value, dict):
        raise SmokeFailure("Expected-state artifact is invalid.")
    return value


def _restore() -> None:
    from patchouli_lib.backup import restore_backup, validate_database, verify_backup_bundle

    _runtime_identity()
    _require_directory_names(DATABASE_PATH.parent, frozenset())
    _require_directory_names(BACKUP_ROOT, frozenset({BUNDLE_PATH.name, EXPECTED_PATH.name}))
    expected = _load_expected()
    manifest = verify_backup_bundle(BUNDLE_PATH, app_version=APP_VERSION)
    if manifest.sha256 != expected.get("backup_sha256"):
        raise SmokeFailure("Expected state does not bind the backup manifest.")
    restore_backup(BUNDLE_PATH, DATABASE_PATH, app_version=APP_VERSION)
    validate_database(DATABASE_PATH)
    DESTINATION_MARKER.write_text("restored-destination-only\n", encoding="ascii")


def _verify() -> None:
    from patchouli_lib.backup import validate_database, verify_backup_bundle

    _runtime_identity()
    _require_directory_names(
        DATABASE_PATH.parent,
        frozenset({DATABASE_PATH.name, DESTINATION_MARKER.name}),
    )
    _require_directory_names(BACKUP_ROOT, frozenset({BUNDLE_PATH.name, EXPECTED_PATH.name}))
    if SOURCE_MARKER.exists() or not DESTINATION_MARKER.is_file():
        raise SmokeFailure("Restored runtime volume isolation failed.")
    report = validate_database(DATABASE_PATH)
    manifest = verify_backup_bundle(BUNDLE_PATH, app_version=APP_VERSION)
    if report.schema_revision != manifest.schema_revision:
        raise SmokeFailure("Restored schema does not match the backup.")
    expected = _load_expected()
    if manifest.sha256 != expected.pop("backup_sha256", None):
        raise SmokeFailure("Restored runtime does not match the backup manifest.")
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        observed = _semantic_state(connection)
    _require_minimum_semantics(observed)
    if observed != expected:
        raise SmokeFailure("Restored semantic state does not exactly match the source.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--internal-phase",
        choices=("seed-backup", "restore", "verify"),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.internal_phase == "seed-backup":
            _seed_backup()
        elif arguments.internal_phase == "restore":
            _restore()
        elif arguments.internal_phase == "verify":
            _verify()
        else:
            timeout = arguments.timeout_seconds
            if isinstance(timeout, bool) or not 1.0 <= timeout <= 3600.0:
                raise SmokeFailure("Container command timeout is invalid.")
            root = Path(__file__).resolve().parents[1]
            run_docker_smoke(root, docker=DockerClient("docker", float(timeout)))
    except SmokeInconclusive as exc:
        print(f"INCONCLUSIVE: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    except SmokeFailure as exc:
        message = str(exc)
        print(f"FAILED: {message}", file=sys.stderr)
        return EXIT_FAILED
    except Exception:
        print("FAILED: Smoke phase failed.", file=sys.stderr)
        return EXIT_FAILED
    print("PASS: synthetic fresh-volume restore completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
