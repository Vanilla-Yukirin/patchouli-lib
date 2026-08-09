# Security Policy

## Supported versions

PatchouliLib does not yet publish a runnable implementation or supported
release. Design flaws and threat-model gaps are still welcome as private
reports when public disclosure could create risk for future adopters.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting flow from the repository's
**Security** tab. Do not open a public issue for a suspected vulnerability.

Include:

- the affected design, component, or version;
- the expected and observed behavior;
- reproduction steps or a minimal proof of concept;
- the likely impact and any known mitigations.

Do not include real credentials, private user documents, or data copied from a
system you do not own. Maintainers will acknowledge a complete report, assess
its impact, and coordinate disclosure before a fix is published.

## Security principles

Future implementations are expected to use least privilege, encrypted
transport, secret rotation, auditable writes, explicit administrative erasure,
and tested backup restoration. Provider-specific deployment hardening belongs
in separate, generic deployment guides rather than project defaults.
