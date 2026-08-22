# Development, validation, and delivery

This document defines the public engineering path. Operator-specific hosts,
paths, ports, domains, and credentials belong outside the repository.

## Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 24 and npm
- Docker with Compose for container validation

## One-command validation

Run all source, test, migration, and documentation checks:

```sh
python scripts/validate.py
```

Add an OCI build and loopback health smoke test:

```sh
python scripts/validate.py --container
```

The container smoke test asks Docker for an ephemeral loopback port. It does not
kill or reuse a process that already owns a preferred port.

The validation contract includes:

1. locked dependency synchronization;
2. Ruff formatting and lint checks;
3. strict MyPy checks;
4. pytest with a 90% minimum coverage gate;
5. Alembic upgrade, downgrade, and repeated upgrade on a temporary database;
6. reproducible npm installation and public Markdown lint;
7. optional image build, migration-on-start, and readiness smoke test.

## Run from source

```sh
uv sync --frozen --all-groups
uv run alembic upgrade head
uv run uvicorn patchouli_lib.app:app --reload
```

The default development database is `data/patchouli.db`. Copy `.env.example` to
an ignored `.env` file when local overrides are needed.

Archive mutation routes are available without a cursor key. The five non-search
retrieval routes are registered only when
`PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET` contains at least 32 UTF-8 bytes.
Generate a deployment-specific random value with a cryptographic random-number
generator and keep it in an ignored environment file or secret store. Production
configuration fails closed when this value is absent or too short. Changing it
invalidates previously issued pagination cursors.

## Packaged synthetic Agent E2E

After the normal validation gate, run the explicit packaged boundary test:

```sh
python scripts/agent_e2e.py
```

The runner builds the server and Python client source distributions, installs
them into separate temporary environments, migrates a temporary SQLite
database, and exercises the installed operator and Agent command-line tools over
ephemeral loopback TLS. It covers bootstrap and scoped Agent provisioning,
archive response-loss replay, Revision history and exact citations, the
explicitly unavailable search contract, credential revocation, and cleanup.

OpenSSL and `uv` must be available on `PATH`. All identities, content,
credentials, cursor keys, certificates, ports, and database state are synthetic
and temporary. Credentials are supplied only through stdin or child-process
environment variables, are never placed in command arguments, and are not
emitted by the runner. The package build and installation run in `uv` offline
mode, and all application traffic stays on loopback. The runner does not contact
a configured deployment and does not make search an implemented feature.

## Run with Compose

```sh
export PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET="$(openssl rand -base64 32)"
docker compose up --build --wait
```

The default listener is loopback-only. If its port is already in use, choose an
explicit free port without terminating the existing process:

```sh
PATCHOULI_PORT=18765 docker compose up --build --wait
```

PowerShell users can generate and set the cursor key for the current process,
then optionally set the port override before running Compose:

```powershell
$bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$env:PATCHOULI_RETRIEVAL_CURSOR_SIGNING_SECRET = [Convert]::ToBase64String($bytes)
$env:PATCHOULI_PORT = 18765
docker compose up --build --wait
```

## Optional web administration

The FastAPI administration console is disabled by default. Enable it only
behind an HTTPS reverse proxy by configuring a generated password verifier, a
distinct session signing secret, and the exact browser origin. The public
Compose file passes these values only when the operator supplies them.

See [Web administration console](admin-web-console.md) for password-verifier
generation, the target-neutral Nginx example, session and CSRF boundaries,
supported local actions, and the explicit absence of deployment authority.

## Database migrations

- Every schema change requires an Alembic revision and tests.
- CI proves a fresh upgrade, downgrade to base, and repeated upgrade.
- Released migrations must remain compatible with the previous application
  image until a stronger rollback contract is accepted.
- Backup and restore acceptance remains required before real knowledge data is
  considered production-ready.

## CI and artifacts

Pull requests run documentation, Python, migration, and container jobs without
deployment credentials. A successful trusted push, or a manual workflow
dispatch against a trusted ref, publishes a `linux/amd64` image to GHCR with:

- an immutable `sha-<commit>` tag;
- an `edge` tag for `main`;
- semantic-version tags for `vX.Y.Z` Git tags;
- a registry-backed build provenance attestation.

Image publishing and release jobs depend on all validation jobs. GitHub Actions
stops after publishing the verified artifacts; it does not connect to a private
runtime.

## Manual private update contract

Private updates are operator-initiated. After a trusted workflow run publishes
an image, an operator obtains its exact `repository@sha256:digest` identity from
the registry or workflow evidence, logs in to the private runtime through an
operator-controlled path, and runs:

```sh
sh deploy/manual-update.sh \
  ghcr.io/example/patchouli-lib@sha256:<64-lowercase-hex-characters>
```

The example repository is synthetic. Do not copy a private registry name,
runtime path, account, or access method into tracked files or public logs.

The local helper requires `PATCHOULI_DEPLOY_ROOT` and
`PATCHOULI_IMAGE_REPOSITORY` in the operator's local environment. Optional
`PATCHOULI_COMPOSE_FILE`, `PATCHOULI_RUNTIME_ENV_FILE`, and
`PATCHOULI_STATE_FILE` values can override the local filenames. Relative
override paths are resolved from `PATCHOULI_DEPLOY_ROOT`; absolute paths remain
absolute. The helper:

1. accepts exactly one image identity as a command-line argument;
2. rejects a different repository or a non-canonical SHA-256 digest;
3. validates the local Compose configuration;
4. pulls and starts only the `api` service, then waits for its health check;
5. atomically records the successful image identity.

It does not read `SSH_ORIGINAL_COMMAND` and does not provide a GitHub-to-runtime
connection. A failed health check does not trigger automatic image rollback:
database migrations can make an older application image incompatible, so an
operator must inspect both application and database state before choosing a
recovery action.

Any future web administration panel must not receive container runtime,
registry, or image-update authority in its first release. Its initial scope is
local application administration, documentation, Agent instructions, and MCP
guidance.

After the workflow change is merged into the default branch, obsolete GitHub
deployment variables, secrets, and Environments may be removed manually. Do
not remove them while a still-active workflow references them.

## Releases

After the project establishes a versioning policy, an annotated `vX.Y.Z` tag on
a verified commit publishes semantic-version image tags and a GitHub Release.
Do not create a release merely to deploy an unreviewed development commit.
