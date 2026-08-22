# ADR 0002: operator-initiated private updates

- Status: Accepted
- Date: 2026-08-23

## Context

GitHub Actions can validate the repository and publish a verifiable container
image without knowing how or where a private instance runs. Connecting from a
public workflow to a private runtime adds long-lived SSH material, target
metadata, another remote execution path, and failure states that are unrelated
to whether the image itself is valid.

The service also owns a database with schema migrations. Automatically starting
an older image after a failed update is not a safe general rollback: the
database may already have moved beyond what that image understands.

## Decision

- GitHub Actions validates the repository, builds the supported container
  image, publishes exact digests to GHCR, and records provenance. It does not
  connect to a private runtime.
- A private update begins only after an operator separately logs in to the
  runtime and explicitly selects an accepted `repository@sha256:digest`.
- `deploy/manual-update.sh` is a local helper. It accepts exactly one image
  argument, validates the configured repository and canonical digest, validates
  local Compose configuration, pulls and starts the API with health checks, and
  records the successful image identity.
- The helper does not read an SSH forced-command variable and does not contain
  target addresses, accounts, paths, credentials, or deployment topology.
- A failed update does not automatically start the previously recorded image.
  Recovery requires an operator to inspect application, migration, and database
  state and to follow an accepted recovery procedure.
- The first release of any web administration panel may expose application
  initialization, local administrative actions, public documentation, Agent
  instructions, and MCP guidance. It must not control the container runtime,
  registry authentication, image selection, update, rollback, or host shell.

## Consequences

- The public workflow needs no private SSH key, host fingerprint, target
  account, port, or deployment enable switch.
- A passing workflow or published image is not proof that a private instance was
  updated. Publication and private operation must be reported as separate
  states.
- Operators perform fewer updates automatically, but each update has an
  explicit human-selected digest and a clear authority boundary.
- Existing GitHub deployment settings become unused only after this change
  reaches the default branch; removal is a separate manual repository
  administration action.
- Database rollback, point-in-time recovery, and live cutover remain governed
  by separately accepted recovery decisions.
