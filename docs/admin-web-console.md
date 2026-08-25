# Web administration console

中文说明见[网页管理面板](admin-web-console.zh-CN.md)。

The optional console provides a small human-facing layer over existing local
operator services. It is not a deployment dashboard and has no host, Docker,
image, rollback, backup-restore, or shell authority.

## Enable it

The console is absent unless all three values are configured:

- `PATCHOULI_ADMIN_PASSWORD_HASH`: a salted password verifier;
- `PATCHOULI_ADMIN_SESSION_SIGNING_SECRET`: a distinct random value containing
  at least 32 UTF-8 bytes;
- `PATCHOULI_ADMIN_ORIGIN`: the one exact browser origin, such as the synthetic
  `https://admin.example.invalid`. Its hostname must already use ASCII or
  Punycode form; raw Unicode hostnames are rejected.

Generate the password verifier locally:

```text
patchouli-admin-password
Administration password:
Confirm administration password:
pbkdf2_sha256$...
```

The command accepts no arguments and suppresses terminal echo when run
interactively. Treat its output as sensitive configuration. Do not place the
password or verifier in a shell argument, tracked file, screenshot, issue, or
log.

When a Compose `.env` file supplies the verifier, enclose the complete value in
single quotes so its `$` separators remain literal. Keep that private `.env`
file outside version control.

Generate the session signing secret independently with a cryptographic random
number generator. Do not reuse the administration password, password verifier,
Agent credential, operator credential, retrieval cursor key, or TLS private
key. The optional `PATCHOULI_ADMIN_SESSION_TTL_SECONDS` is bounded from 300 to
86400 seconds and defaults to 1800.

Production configuration requires an HTTPS origin. Empty values leave the
console disabled, but setting only some values fails application startup.

## Put TLS in front

The public Compose service remains bound to loopback. Copy
`deploy/nginx/patchouli-admin.conf.example` into private operator
configuration, replace its synthetic server name and certificate locations,
and validate Nginx before reloading it.

The Nginx example:

- terminates TLS and forwards the original Host;
- rate-limits the login endpoint;
- limits admin form bodies to 16 KiB;
- keeps the existing Archive request allowance separate; and
- proxies to the loopback API without publishing a target address or
  certificate location in this repository.

The configured `PATCHOULI_ADMIN_ORIGIN` must exactly match what the browser
sends. Do not expose the loopback API directly to an untrusted network to
bypass the TLS and login-rate boundary.

## Use the console

Open `/admin/login` and enter the administration password. The short-lived
session cookie contains only an expiry and random CSRF value.

Use the `中文 / English` control in the upper-right corner to switch the
console, including validation and error messages. The selection is remembered
in a separate, non-sensitive, HttpOnly, SameSite=Strict cookie scoped to
`/admin`. It contains only `zh-CN` or `en`, persists across sign-out, and never
contains or replaces an administration session or bearer credential. The
control is intentionally hidden on a one-time credential response so changing
language cannot accidentally discard the only displayed copy.

The first release provides:

1. one-time Library, Section, Book, and operator initialization;
2. operator credential recovery, which revokes prior active credentials;
3. Agent creation with exact Section permissions and one credential;
4. Agent credential revocation; and
5. read-only operator, Agent, and MCP guidance.

The first-time setup creates the minimum content hierarchy required before a
Page can be stored:

- **Library** is the whole knowledge space. One Library is usually enough for
  personal use. Multiple Libraries are useful when administration and access
  must be separated, but they still share this deployment and database.
- **Section** is a durable large category and the scope used for Agent
  permissions.
- **Book** is a smaller content container inside one Section. Every Page belongs
  to one Book.

Section descriptions and Book summaries are optional human-readable guidance.
The operator name identifies the administrator; audit records link to that
identity rather than using it as the web sign-in name. Credential lifetime
controls the first operator bearer token only; it does not expire the Library
or the web administration password. The default `3600` seconds is one hour.

Operator credentials entered into an action form are used for that request
only. New operator and Agent credentials are rendered once on a no-store page.
Record the accompanying Library, caller, and credential IDs before leaving the
page. A lost response cannot reveal the same token again.

The console does not store bearer values in its cookie or other browser
storage. Do not install browser extensions or logging middleware that captures
form bodies or rendered secrets.
