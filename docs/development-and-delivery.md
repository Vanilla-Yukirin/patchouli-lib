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

Deployment and release jobs depend on all validation jobs.

## Private deployment contract

Private deployment is disabled unless the repository variable
`PRIVATE_DEPLOY_ENABLED` is exactly `true`. The `private-deployment` GitHub
Environment supplies these secrets:

- `DEPLOY_HOST`
- `DEPLOY_PORT`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `DEPLOY_KNOWN_HOSTS`

The workflow sends only the exact GHCR image digest over SSH. A restricted
remote controller validates the configured image repository, pulls it, waits
for Compose health checks, records the successful digest atomically, and tries
the previous digest when health checks fail.

Forks can use the same public workflow without deploying anywhere because the
enable variable and private Environment are absent by default.

A manual workflow dispatch deploys only when it targets `main` and the same
enable variable and private Environment are present. This provides an explicit
redeploy path without weakening pull-request isolation.

## Releases

After the project establishes a versioning policy, an annotated `vX.Y.Z` tag on
a verified commit publishes semantic-version image tags and a GitHub Release.
Do not create a release merely to deploy an unreviewed development commit.
