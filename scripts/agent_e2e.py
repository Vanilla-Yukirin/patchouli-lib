from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, Protocol, cast
from urllib.parse import urlsplit

_COMMAND_TIMEOUT_SECONDS = 180
_SERVER_READY_TIMEOUT_SECONDS = 30.0
_PROCESS_STOP_TIMEOUT_SECONDS = 10.0
_OPERATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.ASCII,
)
_TOKEN = re.compile(r"^plb1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", re.ASCII)


class E2EFailure(RuntimeError):
    """A deliberately redacted end-to-end validation failure."""


class _Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _fail(message: str) -> NoReturn:
    raise E2EFailure(message)


def _clean_environment(source: Mapping[str, str], *, loopback_only: bool = False) -> dict[str, str]:
    environment = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("PATCHOULI_")
        and key.upper() not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "SSL_CERT_FILE"}
    }
    if loopback_only:
        environment = {
            key: value for key, value in environment.items() if not key.upper().endswith("_PROXY")
        }
        environment["NO_PROXY"] = "localhost,127.0.0.1"
    return environment


def _run_checked(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    step: str,
    input_text: str | None = None,
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
    expected_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [str(value) for value in command],
            cwd=cwd,
            env=dict(environment),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail(f"{step} failed.")
    if completed.returncode != expected_exit:
        _fail(f"{step} failed.")
    return completed


def _run_lost_output(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    step: str,
) -> None:
    """Run a command while deliberately discarding its successful stdout response."""

    try:
        completed = subprocess.run(
            [str(value) for value in command],
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail(f"{step} failed.")
    if completed.returncode != 0:
        _fail(f"{step} failed.")


def _parse_json(payload: str, *, step: str) -> dict[str, object]:
    try:
        value: object = json.loads(payload)
    except (json.JSONDecodeError, RecursionError):
        _fail(f"{step} returned an invalid response.")
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{step} returned an invalid response.")
    return cast(dict[str, object], value)


def _object(value: object, *, step: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{step} returned an invalid response.")
    return cast(dict[str, object], value)


def _list(value: object, *, step: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{step} returned an invalid response.")
    return cast(list[object], value)


def _string(value: object, *, step: str) -> str:
    if not isinstance(value, str):
        _fail(f"{step} returned an invalid response.")
    return value


def _boolean(value: object, *, step: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{step} returned an invalid response.")
    return value


def _success_payload(
    completed: subprocess.CompletedProcess[str], *, step: str
) -> dict[str, object]:
    envelope = _parse_json(completed.stdout, step=step)
    if envelope.get("ok") is not True:
        _fail(f"{step} returned an invalid response.")
    return envelope


def _error_payload(completed: subprocess.CompletedProcess[str], *, step: str) -> dict[str, object]:
    envelope = _parse_json(completed.stderr, step=step)
    if envelope.get("ok") is not False:
        _fail(f"{step} returned an invalid response.")
    return _object(envelope.get("error"), step=step)


def _extract_token(completed: subprocess.CompletedProcess[str], *, step: str) -> str:
    token = completed.stdout.removesuffix("\n").removesuffix("\r")
    if not _TOKEN.fullmatch(token):
        _fail(f"{step} did not deliver one valid credential.")
    return token


def _write_private(path: Path, payload: str | bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    if isinstance(payload, bytes):
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    with suppress(OSError):
        os.chmod(path, 0o600)


def _single_artifact(directory: Path) -> Path:
    candidates = tuple(directory.glob("*.tar.gz"))
    if len(candidates) != 1:
        _fail("Package build did not produce exactly one source distribution.")
    return candidates[0]


def _extract_source_distribution(artifact: Path, destination: Path) -> Path:
    destination.mkdir(mode=0o700)
    with tarfile.open(artifact, mode="r:gz") as archive:
        archive.extractall(destination, filter="data")
    roots = tuple(path for path in destination.iterdir() if path.is_dir())
    if len(roots) != 1 or not (roots[0] / "alembic.ini").is_file():
        _fail("Server source distribution did not contain its migration assets.")
    return roots[0]


def _venv_executable(environment: Path, name: str) -> Path:
    directory = environment / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    executable = directory / f"{name}{suffix}"
    if not executable.is_file():
        _fail("An installed package did not expose its expected executable.")
    return executable


def _operation_id_from_state(state: Path) -> str:
    records = tuple((state / "default").glob("*.json"))
    if len(records) != 1 or not _OPERATION_ID.fullmatch(records[0].stem):
        _fail("Lost-response recovery did not preserve one valid operation journal record.")
    return records[0].stem


def _stop_process(process: _Process) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        _fail("Packaged server cleanup failed.")


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def _wait_until_ready(
    endpoint: str,
    *,
    ca_certificate: Path,
    process: _Process,
) -> None:
    parsed_endpoint = urlsplit(endpoint)
    try:
        port = parsed_endpoint.port
    except ValueError:
        _fail("Packaged server readiness endpoint was not loopback HTTPS.")
    if (
        parsed_endpoint.scheme != "https"
        or parsed_endpoint.hostname != "127.0.0.1"
        or port is None
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.path not in {"", "/"}
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        _fail("Packaged server readiness endpoint was not loopback HTTPS.")
    context = ssl.create_default_context(cafile=str(ca_certificate))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )
    deadline = time.monotonic() + _SERVER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _fail("Packaged server stopped before becoming ready.")
        try:
            with opener.open(
                f"{endpoint}/health/ready",
                timeout=2,
            ) as response:
                if response.status == 200:
                    return
        except (OSError, ssl.SSLError, urllib.error.URLError):
            time.sleep(0.1)
    _fail("Packaged server did not become ready in time.")


def _build_certificates(
    openssl: str,
    directory: Path,
    *,
    environment: Mapping[str, str],
) -> tuple[Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca_certificate = directory / "ca.crt"
    server_key = directory / "server.key"
    request = directory / "server.csr"
    server_certificate = directory / "server.crt"
    extensions = directory / "server.ext"
    ca_configuration = directory / "ca.cnf"
    server_configuration = directory / "server.cnf"
    _write_private(
        ca_configuration,
        "[req]\n"
        "distinguished_name=dn\n"
        "x509_extensions=v3_ca\n"
        "prompt=no\n"
        "[dn]\n"
        "CN=PatchouliLib Synthetic E2E CA\n"
        "[v3_ca]\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n",
    )
    _write_private(
        server_configuration,
        "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=localhost\n",
    )
    _write_private(
        extensions,
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
    )
    _run_checked(
        (
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            ca_key,
            "-out",
            ca_certificate,
            "-days",
            "1",
            "-sha256",
            "-config",
            ca_configuration,
        ),
        cwd=directory,
        environment=environment,
        step="Ephemeral CA generation",
    )
    _run_checked(
        (
            openssl,
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            server_key,
            "-out",
            request,
            "-config",
            server_configuration,
        ),
        cwd=directory,
        environment=environment,
        step="Ephemeral server key generation",
    )
    _run_checked(
        (
            openssl,
            "x509",
            "-req",
            "-in",
            request,
            "-CA",
            ca_certificate,
            "-CAkey",
            ca_key,
            "-CAcreateserial",
            "-out",
            server_certificate,
            "-days",
            "1",
            "-sha256",
            "-extfile",
            extensions,
        ),
        cwd=directory,
        environment=environment,
        step="Ephemeral server certificate signing",
    )
    for path in (ca_key, server_key):
        with suppress(OSError):
            os.chmod(path, 0o600)
    return ca_certificate, server_certificate, server_key


def _build_and_install(
    root: Path,
    temporary: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
) -> tuple[Path, Path, Path, Path]:
    server_dist = temporary / "server-dist"
    client_dist = temporary / "client-dist"
    server_dist.mkdir(mode=0o700)
    client_dist.mkdir(mode=0o700)
    _run_checked(
        (uv, "build", "--sdist", "--offline", "--out-dir", server_dist),
        cwd=root,
        environment=environment,
        step="Server source distribution build",
    )
    _run_checked(
        (uv, "build", "--sdist", "--offline", "--out-dir", client_dist),
        cwd=root / "clients" / "python",
        environment=environment,
        step="Client source distribution build",
    )

    server_artifact = _single_artifact(server_dist)
    server_source = _extract_source_distribution(
        server_artifact,
        temporary / "server-source",
    )
    client_artifact = _single_artifact(client_dist)
    server_environment = temporary / "server-environment"
    client_environment = temporary / "client-environment"
    _run_checked(
        (uv, "venv", "--python", sys.executable, server_environment),
        cwd=temporary,
        environment=environment,
        step="Server virtual environment creation",
    )
    _run_checked(
        (uv, "venv", "--python", sys.executable, client_environment),
        cwd=temporary,
        environment=environment,
        step="Client virtual environment creation",
    )
    server_python = _venv_executable(server_environment, "python")
    client_python = _venv_executable(client_environment, "python")
    _run_checked(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            server_python,
            server_artifact,
        ),
        cwd=temporary,
        environment=environment,
        step="Server source distribution install",
    )
    _run_checked(
        (
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            client_python,
            client_artifact,
        ),
        cwd=temporary,
        environment=environment,
        step="Client source distribution install",
    )
    return (
        server_python,
        _venv_executable(server_environment, "patchouli-operator"),
        (_venv_executable(client_environment, "patchouli")),
        server_source,
    )


def _cli_success(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    step: str,
) -> dict[str, object]:
    completed = _run_checked(
        (executable, "--output", "json", *arguments),
        cwd=cwd,
        environment=environment,
        step=step,
    )
    return _success_payload(completed, step=step)


def _require_equal(actual: object, expected: object, *, step: str) -> None:
    if actual != expected:
        _fail(f"{step} did not preserve the accepted contract.")


def _exercise_agent(
    patchouli: Path,
    operator: Path,
    *,
    runtime: Path,
    endpoint: str,
    ca_certificate: Path,
    server_environment: Mapping[str, str],
    operator_token: str,
    agent_token: str,
) -> None:
    state = runtime / "client-state"
    inputs = runtime / "client-inputs"
    state.mkdir(mode=0o700)
    inputs.mkdir(mode=0o700)
    client_environment = _clean_environment(os.environ, loopback_only=True)
    client_environment.update(
        {
            "PATCHOULI_ENDPOINT": endpoint,
            "PATCHOULI_API_VERSION": "v1",
            "PATCHOULI_TOKEN": agent_token,
            "PATCHOULI_STATE_DIR": str(state),
            "PATCHOULI_INPUT_ROOT": str(inputs),
            "SSL_CERT_FILE": str(ca_certificate),
        }
    )

    capabilities = _cli_success(
        patchouli,
        ("capabilities",),
        cwd=inputs,
        environment=client_environment,
        step="Agent capabilities",
    )
    capability_data = _object(capabilities.get("data"), step="Agent capabilities")
    _require_equal(capability_data.get("features"), ["archive", "retrieval"], step="Capabilities")

    whoami = _cli_success(
        patchouli,
        ("whoami",),
        cwd=inputs,
        environment=client_environment,
        step="Agent identity",
    )
    identity = _object(whoami.get("data"), step="Agent identity")
    caller_id = _string(identity.get("caller_id"), step="Agent identity")
    credential_id = _string(identity.get("credential_id"), step="Agent identity")
    grants = _list(identity.get("grants"), step="Agent identity")
    if len(grants) != 1:
        _fail("Agent identity did not contain one exact Section grant set.")
    grant = _object(grants[0], step="Agent identity")
    section_id = _string(grant.get("section_id"), step="Agent identity")
    _require_equal(
        grant.get("actions"),
        ["archive:write", "page:read", "section:query"],
        step="Agent grants",
    )

    sections = _cli_success(
        patchouli,
        ("sections", "list"),
        cwd=inputs,
        environment=client_environment,
        step="Section listing",
    )
    section_items = _list(
        _object(sections.get("data"), step="Section listing").get("items"), step="Section listing"
    )
    _require_equal(len(section_items), 1, step="Section listing")
    _require_equal(
        _object(section_items[0], step="Section listing").get("section_id"),
        section_id,
        step="Section listing",
    )

    books = _cli_success(
        patchouli,
        ("books", "list", "--section", section_id),
        cwd=inputs,
        environment=client_environment,
        step="Book listing",
    )
    book_items = _list(
        _object(books.get("data"), step="Book listing").get("items"), step="Book listing"
    )
    _require_equal(len(book_items), 1, step="Book listing")
    book_id = _string(
        _object(book_items[0], step="Book listing").get("book_id"), step="Book listing"
    )

    create_metadata = inputs / "create.json"
    create_content = inputs / "create.md"
    revise_metadata = inputs / "revise.json"
    revise_content = inputs / "revise.md"
    query = inputs / "query.txt"
    first_body = "# Synthetic packaged archive\n"
    second_body = "# Synthetic packaged archive\n\nRevision two.\n"
    _write_private(
        create_metadata,
        json.dumps(
            {
                "title": "Synthetic packaged archive",
                "occurred_at": "2026-08-13T12:00:00.123456Z",
                "source": {"kind": "synthetic-e2e"},
            },
            separators=(",", ":"),
        ),
    )
    _write_private(create_content, first_body)
    _write_private(
        revise_metadata,
        json.dumps({"source": {"kind": "synthetic-e2e-revision"}}, separators=(",", ":")),
    )
    _write_private(revise_content, second_body)
    _write_private(query, "synthetic unavailable search\n")

    create_arguments = (
        "--input-root",
        str(inputs),
        "archive",
        "create",
        "--section",
        section_id,
        "--book",
        book_id,
        "--metadata-file",
        create_metadata.name,
        "--content-file",
        create_content.name,
    )
    _run_lost_output(
        (patchouli, "--output", "json", *create_arguments),
        cwd=inputs,
        environment=client_environment,
        step="Lost-response archive creation",
    )
    operation_id = _operation_id_from_state(state)
    replay = _cli_success(
        patchouli,
        (*create_arguments, "--operation-id", operation_id),
        cwd=inputs,
        environment=client_environment,
        step="Lost-response archive replay",
    )
    replay_metadata = _object(replay.get("metadata"), step="Lost-response archive replay")
    _require_equal(
        _boolean(replay_metadata.get("idempotency_replayed"), step="Lost-response archive replay"),
        True,
        step="Lost-response archive replay",
    )
    first_etag = _string(replay_metadata.get("etag"), step="Lost-response archive replay")
    replay_document = _object(replay.get("data"), step="Lost-response archive replay")
    replay_page = _object(replay_document.get("page"), step="Lost-response archive replay")
    page_id = _string(replay_page.get("page_id"), step="Lost-response archive replay")
    first_citation = _object(replay_document.get("citation"), step="Lost-response archive replay")
    _require_equal(first_citation.get("revision_number"), 1, step="Archive create citation")

    current_one = _cli_success(
        patchouli,
        ("page", "current", "--section", section_id, "--page", page_id),
        cwd=inputs,
        environment=client_environment,
        step="Current Page after replay",
    )
    current_one_metadata = _object(current_one.get("metadata"), step="Current Page after replay")
    _require_equal(current_one_metadata.get("etag"), first_etag, step="Replay ETag")

    revised = _cli_success(
        patchouli,
        (
            "--input-root",
            str(inputs),
            "archive",
            "revise",
            "--section",
            section_id,
            "--page",
            page_id,
            "--if-match",
            first_etag,
            "--metadata-file",
            revise_metadata.name,
            "--content-file",
            revise_content.name,
        ),
        cwd=inputs,
        environment=client_environment,
        step="Archive revision",
    )
    revised_metadata = _object(revised.get("metadata"), step="Archive revision")
    revised_etag = _string(revised_metadata.get("etag"), step="Archive revision")
    if revised_etag == first_etag:
        _fail("Archive revision did not advance the strong ETag.")
    revised_document = _object(revised.get("data"), step="Archive revision")
    revised_citation = _object(revised_document.get("citation"), step="Archive revision")
    _require_equal(revised_citation.get("revision_number"), 2, step="Revision citation")

    pages = _cli_success(
        patchouli,
        ("pages", "list", "--section", section_id),
        cwd=inputs,
        environment=client_environment,
        step="Page metadata listing",
    )
    page_items = _list(
        _object(pages.get("data"), step="Page metadata listing").get("items"),
        step="Page metadata listing",
    )
    _require_equal(len(page_items), 1, step="Page metadata listing")
    page_item = _object(page_items[0], step="Page metadata listing")
    listed_page = _object(page_item.get("page"), step="Page metadata listing")
    listed_citation = _object(page_item.get("citation"), step="Page metadata listing")
    _require_equal(listed_page.get("page_id"), page_id, step="Page metadata identity")
    _require_equal(listed_citation, revised_citation, step="Page exact citation")

    current = _cli_success(
        patchouli,
        ("page", "current", "--section", section_id, "--page", page_id),
        cwd=inputs,
        environment=client_environment,
        step="Current Page fetch",
    )
    current_metadata = _object(current.get("metadata"), step="Current Page fetch")
    _require_equal(current_metadata.get("etag"), revised_etag, step="Current Page ETag")
    current_document = _object(current.get("data"), step="Current Page fetch")
    _require_equal(current_document.get("citation"), revised_citation, step="Current Page citation")
    current_revision = _object(current_document.get("revision"), step="Current Page fetch")
    _require_equal(current_revision.get("content"), second_body, step="Current Page content")

    history = _cli_success(
        patchouli,
        ("page", "revision", "--section", section_id, "--page", page_id, "--revision", "1"),
        cwd=inputs,
        environment=client_environment,
        step="Exact Revision fetch",
    )
    history_document = _object(history.get("data"), step="Exact Revision fetch")
    _require_equal(history_document.get("citation"), first_citation, step="Historical citation")
    historical_revision = _object(history_document.get("revision"), step="Exact Revision fetch")
    _require_equal(historical_revision.get("content"), first_body, step="Historical content")

    search = _run_checked(
        (
            patchouli,
            "--output",
            "json",
            "--input-root",
            inputs,
            "section",
            "search",
            "--section",
            section_id,
            "--query-file",
            query.name,
        ),
        cwd=inputs,
        environment=client_environment,
        step="Unavailable search",
        expected_exit=16,
    )
    search_error = _error_payload(search, step="Unavailable search")
    _require_equal(search_error.get("code"), "search_unavailable", step="Unavailable search")

    revoke = _run_checked(
        (
            operator,
            "revoke-agent-credential",
            "--library-name",
            "Synthetic E2E Library",
            "--caller-id",
            caller_id,
            "--credential-id",
            credential_id,
        ),
        cwd=runtime,
        environment=server_environment,
        input_text=f"{operator_token}\n",
        step="Agent credential revocation",
    )
    if revoke.stdout or revoke.stderr:
        _fail("Agent credential revocation emitted unexpected output.")

    revoked = _run_checked(
        (patchouli, "--output", "json", "capabilities"),
        cwd=inputs,
        environment=client_environment,
        step="Revoked credential rejection",
        expected_exit=10,
    )
    revoked_error = _error_payload(revoked, step="Revoked credential rejection")
    _require_equal(revoked_error.get("code"), "invalid_token", step="Revoked credential rejection")


def run() -> None:
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    openssl = shutil.which("openssl")
    if uv is None:
        _fail("The packaged E2E requires uv on PATH.")
    if openssl is None:
        _fail("The packaged E2E requires OpenSSL on PATH.")
    base_environment = _clean_environment(os.environ)

    with tempfile.TemporaryDirectory(prefix="patchouli-agent-e2e-") as raw_temporary:
        temporary = Path(raw_temporary)
        with suppress(OSError):
            os.chmod(temporary, 0o700)
        server_python, operator, patchouli, server_source = _build_and_install(
            root,
            temporary,
            uv=uv,
            environment=base_environment,
        )
        database = temporary / "synthetic.db"
        cursor_secret = secrets.token_urlsafe(32)
        server_environment = dict(base_environment)
        server_environment.update(
            {
                "PATCHOULI_ENVIRONMENT": "test",
                "PATCHOULI_DATABASE_URL": f"sqlite:///{database.as_posix()}",
                "PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET": cursor_secret,
            }
        )
        _run_checked(
            (
                server_python,
                "-m",
                "alembic",
                "-c",
                server_source / "alembic.ini",
                "upgrade",
                "head",
            ),
            cwd=server_source,
            environment=server_environment,
            step="Temporary database migration",
        )

        bootstrap = _run_checked(
            (
                operator,
                "bootstrap",
                "--library-name",
                "Synthetic E2E Library",
                "--section-name",
                "Synthetic E2E Section",
                "--book-name",
                "Synthetic E2E Book",
                "--operator-name",
                "Synthetic E2E Operator",
                "--credential-ttl-seconds",
                "3600",
            ),
            cwd=temporary,
            environment=server_environment,
            step="Local operator bootstrap",
        )
        operator_token = _extract_token(bootstrap, step="Local operator bootstrap")
        provision = _run_checked(
            (
                operator,
                "provision-agent",
                "--library-name",
                "Synthetic E2E Library",
                "--section-name",
                "Synthetic E2E Section",
                "--agent-name",
                "Synthetic E2E Agent",
                "--credential-ttl-seconds",
                "3600",
                "--grant",
                "archive:write",
                "--grant",
                "page:read",
                "--grant",
                "section:query",
            ),
            cwd=temporary,
            environment=server_environment,
            input_text=f"{operator_token}\n",
            step="Local Agent provision",
        )
        agent_token = _extract_token(provision, step="Local Agent provision")

        certificate_directory = temporary / "certificates"
        certificate_directory.mkdir(mode=0o700)
        ca_certificate, server_certificate, server_key = _build_certificates(
            openssl,
            certificate_directory,
            environment=base_environment,
        )
        port = _loopback_port()
        endpoint = f"https://127.0.0.1:{port}"
        process = subprocess.Popen(
            (
                str(server_python),
                "-m",
                "uvicorn",
                "patchouli_lib.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--ssl-certfile",
                str(server_certificate),
                "--ssl-keyfile",
                str(server_key),
                "--log-level",
                "warning",
                "--no-access-log",
            ),
            cwd=temporary,
            env=server_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_until_ready(endpoint, ca_certificate=ca_certificate, process=process)
            _exercise_agent(
                patchouli,
                operator,
                runtime=temporary,
                endpoint=endpoint,
                ca_certificate=ca_certificate,
                server_environment=server_environment,
                operator_token=operator_token,
                agent_token=agent_token,
            )
        finally:
            _stop_process(process)
            del agent_token, operator_token


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the packaged PatchouliLib synthetic Agent E2E over loopback TLS."
    )
    parser.parse_args(argv)
    try:
        run()
    except E2EFailure as exc:
        print(f"Packaged Agent E2E failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Packaged Agent E2E interrupted.", file=sys.stderr)
        return 130
    except Exception:
        print(
            "Packaged Agent E2E failed closed without exposing internal details.", file=sys.stderr
        )
        return 1
    print("Packaged Agent E2E passed with synthetic temporary state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
