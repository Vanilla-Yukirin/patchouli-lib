# ADR 0003: bounded web administration console

- Status: Accepted
- Date: 2026-08-23

## Context

The local operator CLI provides safe initialization, recovery, Agent
provisioning, and credential revocation, but it is inconvenient for occasional
human administration. A small browser interface can make these operations and
the public Agent/MCP guidance easier to discover.

A browser also creates a new attack surface. The existing local operator
boundary must not silently become a remote shell or deployment controller, and
passwords and bearer credentials must not enter URLs, logs, browser storage, or
long-lived web sessions.

## Decision

- The administration console is optional and lives under `/admin` in the
  existing FastAPI application. It is absent unless a password verifier,
  independent session signing secret, and exact browser origin are all
  configured.
- The server stores a salted PBKDF2-SHA256 password verifier, never the
  administration password. `patchouli-admin-password` reads and confirms the
  password without accepting it as a command-line argument.
- Successful login creates a short-lived, signed, HttpOnly, SameSite=Strict
  cookie. The cookie contains only an expiry and random CSRF value. Production
  configuration requires an HTTPS origin and emits a Secure cookie.
- Every state-changing request requires the configured Host and Origin, a valid
  session, and the matching CSRF value. Form bodies and field counts are
  bounded; unknown and ambiguous duplicate fields fail closed.
- Operator and Agent bearer values appear only in password-form inputs or a
  no-store one-time result page. They are not added to the session, URL, log,
  or persistent web state.
- The console calls the existing transaction, authorization, and audit
  services. It supports first initialization, operator recovery, scoped Agent
  provisioning, and Agent credential revocation.
- The console includes read-only operator, Agent, and MCP guidance. It does not
  control images, registries, updates, rollback, Docker, host shells, backup
  restore, secrets infrastructure, or deployment.
- A target-neutral Nginx example terminates TLS, preserves the exact Host and
  Origin contract, bounds admin form bodies, and rate-limits login requests.
  Private names, certificates, paths, and network exposure remain
  operator-owned configuration.

## Consequences

- Enabling the console requires three deliberate configuration values and a
  TLS reverse proxy. The API remains loopback-only in the public Compose
  example.
- Losing an operator or Agent credential response does not make the value
  recoverable. The operator must revoke or recover using the recorded public
  credential metadata and normal audited procedures.
- The password verifier and session signing secret remain sensitive even
  though neither is a usable browser password. They must stay in the private
  secret store and out of tracked files and logs.
- More advanced administration, user accounts, multiple roles, secret
  rotation workflows, and deployment control require separate decisions.
