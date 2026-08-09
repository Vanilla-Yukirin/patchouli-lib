# Authentication, authorization, and audit

## Credential classes

The design distinguishes:

- an administrative credential for credential management, policy changes,
  export, and exceptional erasure;
- scoped caller credentials for CLI tools, MCP clients, services, and agents.

Administrative credentials must not be used for ordinary reads and writes.
Caller credentials need names and descriptions for auditability, but examples
must use synthetic identities.

## Credential handling

Future implementations must:

- accept credentials only over encrypted transport;
- store verifiers rather than recoverable plaintext tokens;
- show a token value only at creation time;
- support expiry, rotation, revocation, and last-used metadata;
- avoid logging raw authorization headers or secrets;
- use constant-time verification where relevant.

## Authorization scope

Section and Tag constraints are candidate policy dimensions. Book- and Page-level
rules are intentionally deferred until concrete use cases justify the added
complexity.

The recommended baseline is default-deny for new caller credentials: no content
access until an explicit scope is assigned. The exact allow/deny precedence and
behavior of untagged content remain open design questions.

Authorization must be evaluated before retrieval, summary generation, derived
fact access, reference traversal, and organization suggestions. Filtering only
the final response is insufficient.

## Audit events

Every accepted mutation should record:

- actor identity and credential identifier;
- action and affected resource;
- request or correlation identifier;
- timestamp and result;
- previous and new Revision or policy version when applicable.

Audit data must not contain raw credentials or full private document bodies.
Retention, export, and integrity guarantees require a dedicated proposal.

## Threat model topics

- stolen caller or administrative credentials;
- prompt injection in stored content;
- cross-Section data leakage during retrieval;
- unauthorized provider calls during summarization;
- malicious Markdown or reference expansion;
- denial of service through expensive search, upload, or model requests;
- backup leakage and incomplete administrative erasure.
