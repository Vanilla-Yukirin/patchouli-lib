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
  `https://admin.example.invalid`.

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

The first release provides:

1. one-time Library, Section, Book, and operator initialization;
2. operator credential recovery, which revokes prior active credentials;
3. Agent creation with exact Section permissions and one credential;
4. Agent credential revocation; and
5. read-only operator, Agent, and MCP guidance.

Operator credentials entered into an action form are used for that request
only. New operator and Agent credentials are rendered once on a no-store page.
Record the accompanying Library, caller, and credential IDs before leaving the
page. A lost response cannot reveal the same token again.

The console does not store bearer values in its cookie or other browser
storage. Do not install browser extensions or logging middleware that captures
form bodies or rendered secrets.
