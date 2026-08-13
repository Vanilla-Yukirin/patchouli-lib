# SQLite backup, restore, and upgrade for alpha

Status: **Proposed**

## Decision request and gate

Adopt the lifecycle and safety boundaries in this document as the alpha design
for whole-database SQLite backup, restore into a new data slot, and schema
upgrade. The proposal prefers SQLite's online backup API for a live source,
publishes an immutable bundle only after its manifest is complete, and keeps an
old application image paired with its untouched old data slot during an
upgrade.

**This proposal is not accepted.** Merging this document, landing an
experimental backup library, or passing a synthetic restore drill does not make
real-data deployment or point-in-time recovery production-ready. Acceptance
requires a public maintainer-reviewed status change. The point-in-time recovery
branch additionally remains blocked on explicit credential-quarantine and
identifier/idempotency anti-reuse decisions described below.

This document defines a portable application contract. It does not identify a
deployment host, storage path, domain, credential, secret value, or private
topology.

## Scope and terminology

Several operations use the word "restore" but have different authorities and
failure modes. They must not share an ambiguous command or readiness claim.

| Operation | Meaning | Preserves operational database state? | Alpha position |
| --- | --- | --- | --- |
| Page Revision restore | Create a new Revision whose body derives from an older Revision, then advance the Page's current pointer in the normal mutation transaction. | Yes; prior history remains linear and immutable. | Already the public content-recovery direction; not a database restore. |
| Whole-database backup | Capture one consistent SQLite snapshot plus bounded metadata needed to validate it. | Yes, for the exact captured point. | Proposed here. |
| Whole-database restore | Validate a backup bundle and materialize it into a new, empty destination without changing the live database. | Yes, subject to exact-schema and semantic validation. | Proposed here. |
| Image rollback | Start an older application image against the current data slot. | No guarantee; the schema or semantics may already have advanced. | Not a database-recovery mechanism. |
| Image-and-data-slot rollback | Return to an immutable old image and the exact untouched old data slot that it previously served. | Yes only before the candidate has accepted a durable write. | Proposed upgrade safety mechanism. |
| Export | Produce an application-level, interoperable representation of selected content and metadata. | Not necessarily; credentials, audit, idempotency, migration state, and derived data may be absent. | Required separately; not a substitute for backup. |

An **image** is an immutable application artifact. A **data slot** is a database
location owned by exactly one active image generation at a time. A **backup
bundle** is an immutable directory containing one SQLite snapshot and one
manifest. A **backup receipt** is an operator-held record that a named bundle
was completed and passed the required validations; it is a deployment gate,
not the backup payload itself.

## Proposed alpha invariants

### Capture and immutable bundle

1. A live database is copied through SQLite, not by directly copying an open
   main database file. The alpha implementation uses the SQLite online backup
   API exposed by Python's `sqlite3.Connection.backup()`.
2. Source and destination are distinct database connections and distinct
   filesystem objects. The destination must be newly created in a staging
   directory. Existing files, non-empty directories, aliases of the source,
   symbolic links, reparse points, and other path indirections fail closed.
3. The completed SQLite snapshot represents one consistent source state even
   when other connections can write while copying. Implementations must still
   impose bounded busy handling, runtime, and resource use; an indefinitely
   restarting copy is a failure, not progress.
4. The snapshot is closed and validated before publication. Validation opens
   it query-only and must not repair, migrate, vacuum, normalize, or otherwise
   mutate the candidate artifact.
5. A bundle has a strict, versioned manifest with bounded field sizes, canonical
   encoding, an exact database-content digest, the exact Alembic head, the
   validation-contract version, completion time in canonical UTC, and the
   validation result. Unknown fields, duplicate keys, non-finite numbers,
   malformed timestamps, and non-canonical encodings are rejected.
6. The manifest is written last. Only after the database and manifest are
   durable where the platform supports the required synchronization may the
   staging directory be renamed to its final name. Readers treat a missing,
   invalid, or non-final manifest as an unpublished bundle.
7. Directory rename and manifest-last publication prevent a cooperative reader
   from accepting an incomplete bundle; they do not create a general
   multi-file atomicity guarantee across every filesystem, object store, or
   power-loss boundary. A storage adapter must document and test its own
   publication semantics.
8. Once published, neither the database nor manifest is changed in place. A new
   validation result or storage transformation creates a new bundle identity.
   Restore never overwrites a published backup.

SQLite documents that its online backup API produces a snapshot while locking
the live source only during individual read steps. Python 3.13 exposes that API
as `Connection.backup()` and supports a database concurrently accessed by
other clients. SQLite also warns that copying an active database file directly
can produce mixed state, and that a WAL or hot journal is part of database
state. These are the reasons the alpha contract forbids a raw live-file copy.

### Integrity, authenticity, and confidentiality

The manifest digest detects accidental truncation, substitution, or corruption
only when the manifest itself is trusted. An attacker able to replace both the
database and manifest can compute a new ordinary digest. Manifest-last is a
completion protocol, not an authenticity proof, and a creation timestamp is
not a freshness or anti-rollback proof.

The alpha bundle format therefore has two explicit trust boundaries:

- local integrity validation may rely on a cryptographic digest inside the
  manifest when the containing storage is already trusted; and
- storage outside that trust boundary requires an independently protected MAC,
  signature, or equivalent authenticated catalog before it may become a
  production recovery source.

The exact authenticity mechanism, key custody, retention policy, and
rollback-resistant catalog are open decisions. Until they are accepted, a
bundle from an untrusted or replayable location is not eligible for automatic
restore.

A whole-database backup has the confidentiality and administrative-erasure
impact of the live database. It contains content, history, Sources, caller and
grant records, credential verifiers, audit events, and idempotency responses.
Encryption at rest, access policy, copying, retention, destruction, and support
handling must be at least as restrictive as for live data. A verifier is not a
plaintext bearer token, but it is still security-sensitive. Logs, receipts,
and error messages must never contain document bodies, credential material, or
manifest fields that reveal operator-specific infrastructure.

### Exact schema compatibility and semantic validation

A backup records one exact Alembic head. An ordinary restore validator accepts
only the exact schema head it was built to validate. It does not guess a
migration path, start an older image against newer data, or silently upgrade a
restored database.

Validation must fail closed unless all applicable checks pass, including:

- `PRAGMA integrity_check` returns exactly `ok` and
  `PRAGMA foreign_key_check` returns no rows;
- required tables, indexes, and critical triggers match the exact expected
  schema rather than merely sharing an Alembic version string;
- every Page current pointer names an exact Revision of the same Page, Revision
  numbers are contiguous from one, and no pending append guard remains;
- stored content size and digest match the Revision bytes, and every Source is
  associated with its exact Page and Revision;
- canonical and alias identifier reservations remain attached to one Page, and
  collision counters are at least the high-water mark implied by permanent
  reservations;
- bootstrap, caller, credential-rotation, grant, and audit relationships are
  internally consistent without credential cycles or impossible lifetimes;
- every durable idempotency response remains parseable and agrees with its
  stored method, route, Page, Revision, citation, Location, and entity tag; and
- any accepted derived-state version is either compatible and valid or is
  declared unavailable until a deterministic rebuild succeeds.

An implementation may add stricter checks without weakening these invariants.
It must version the validation contract so a receipt identifies what was
actually proved.

### Restore only into a new empty destination

Restore materializes a validated snapshot into a new, empty data slot. It never
overwrites the live database, a previous slot, or another backup. The restore
destination is subject to the same alias and path-indirection checks as backup.

After materialization, the restored database is validated again from the
destination. The restore succeeds only when the copied bytes, exact schema, and
semantic checks match the bundle contract. Success means "validated inactive
data slot"; it does not authorize traffic, migration, or credential use.

## Lossless upgrade workflow

The alpha upgrade path is copy-and-promote, not an in-place migration with an
image-only rollback:

1. Identify the currently serving pair: old immutable image plus old data slot.
2. Complete a whole-database backup and its validation.
3. Persist a pre-migration backup receipt containing the immutable bundle
   identity, manifest digest, source schema head, validation-contract version,
   and completed result. The controller must be able to read the receipt after
   a failed deployment process.
4. Restore that bundle into a fresh candidate data slot. The old slot remains
   untouched and owned by the old image.
5. Run the candidate image's migration against only the candidate slot.
6. Run exact-schema validation, application readiness, and synthetic read-only
   probes against the candidate while it is isolated from ordinary writes.
7. Switch service from the old pair to the candidate image-and-slot pair.
8. Record whether the candidate has accepted its first durable write.

The pre-migration receipt is mandatory. A file that merely exists, an
unvalidated digest, or an old successful receipt for different source bytes
does not satisfy the gate.

This flow is a **lossless upgrade copy** when the backup point is the latest
committed authoritative state selected for cutover and no write accepted by the
old pair is omitted. A deployment may require a bounded write quiescence or an
equivalent final synchronization step; the exact availability technique is an
implementation decision that must still prove the no-omitted-write invariant.

### Pairing and rollback boundary

An old image is rolled back only with its exact untouched old data slot. An old
image must never be paired automatically with a candidate slot whose migration
ran, even if migration downgrade tests pass. The old pair remains isolated and
immutable while it is eligible for rollback.

Before the candidate accepts a durable write, a failed health or cutover check
may stop the candidate and switch back to the old pair. After the candidate
accepts any durable write, automatic rollback to the old pair is forbidden:
the old slot does not contain that write, and serving it would silently fork
history, resurrect prior security state, or replay an idempotent mutation under
different facts. At that point the safe choices are forward repair, a reviewed
reconciliation into a new slot, or an explicitly authorized disaster-recovery
procedure. Both slots are preserved until that decision is complete.

## Failure state machine

```text
SERVING_OLD_PAIR
  -> CAPTURING_BACKUP
  -> VERIFIED_BACKUP_RECEIPT
  -> RESTORED_FRESH_CANDIDATE_SLOT
  -> MIGRATED_AND_VALIDATED_CANDIDATE
  -> CANDIDATE_CUTOVER_NO_WRITES
  -> CANDIDATE_HAS_DURABLE_WRITES
```

- Failure in `CAPTURING_BACKUP` publishes no bundle and leaves the old pair
  serving.
- Failure after `VERIFIED_BACKUP_RECEIPT` but before cutover destroys or
  quarantines only the candidate slot; the old pair remains authoritative.
- Failure in `CANDIDATE_CUTOVER_NO_WRITES` may switch back to the old pair after
  proving that the candidate accepted no durable write.
- Failure in `CANDIDATE_HAS_DURABLE_WRITES` stops automatic rollback. Preserve
  both slots, prevent dual writers, and require reviewed forward recovery or
  reconciliation.
- Loss of the controller's local process does not erase the durable state. On
  restart it must infer the phase from authenticated receipts and explicit slot
  ownership, not from a guessed current image tag.
- An unknown phase, missing receipt, ambiguous slot owner, or incomplete
  publication fails closed and requires operator review.

Every transition must be idempotent or detect that a different operation is
already in progress. No failure path may delete the last validated backup or
the last known-good image-and-slot pair.

## Point-in-time recovery is a separate security event

A **point-in-time recovery (PITR)** intentionally selects a snapshot older than
the latest committed state. It is not the lossless upgrade copy above. The
missing interval can contain content mutations, Page-ID reservations,
idempotency responses, caller or grant changes, credential rotation and
revocation, and audit events.

A restored older snapshot must enter `PITR_QUARANTINED`, with no Agent or public
traffic and no ordinary write authority. It cannot become serviceable until all
of the following are implemented and proved:

1. **Credential and caller quarantine.** Every restored caller, credential,
   grant, and bootstrap authority is treated as untrusted historical state.
   The recovery procedure disables or replaces it before network exposure and
   proves that credentials valid before or after the recovery point cannot be
   resurrected accidentally.
2. **Operation-journal quarantine.** Client-side or external operation journals
   from either side of the recovery boundary cannot replay automatically. They
   require reconciliation against the selected authoritative timeline.
3. **Cursor quarantine.** Pagination cursor signing material is replaced so
   cursors issued against either abandoned state cannot be replayed against the
   recovered state.
4. **External discontinuity record.** An append-only record outside the restored
   database identifies the selected backup, abandoned interval, old and new
   state identities, validation result, and authorization for recovery. The
   record contains no secrets or document bodies and is not rolled backward
   with the database.
5. **Identifier and idempotency anti-reuse.** Page identifier reservations,
   collision-counter high-water marks, and idempotency key digests created
   after the recovery point must not be forgotten and reused for different
   Pages, Revisions, callers, requests, or responses.

The fifth item has no accepted mechanism today. Possible future approaches
include an independently durable monotonic reservation ledger or a reviewed
merge of high-water security metadata from the abandoned state. Each has
privacy, authenticity, and availability trade-offs. This proposal does not
choose one. Therefore an older PITR snapshot may be inspected or used to build
a reviewed recovery candidate, but it must not serve writes while this proposal
is Proposed and the anti-reuse decision is unresolved.

## Threat model and security impact

The alpha design must address at least these failure and attack classes:

- a live raw-file copy that omits or mismatches a WAL or hot journal;
- a crash, full disk, cancellation, or power loss that leaves a partial bundle;
- source/destination aliasing, path traversal, symlink, hard-link, reparse-point,
  mount, or race attacks that overwrite live data or escape an allowed root;
- restoration over an existing database or into a slot still owned by a
  running process;
- a corrupt database that passes a file digest because the corrupt bytes were
  captured consistently;
- a forged manifest, bundle replay, or authenticated catalog rollback;
- schema-version spoofing that hides missing constraints, indexes, or triggers;
- maliciously large manifests, database files, row values, validation result
  sets, or unbounded backup retries;
- backup disclosure, insecure temporary permissions, secret-bearing logs, or
  forgotten copies after administrative erasure;
- image-only rollback after schema migration;
- dual writers during slot switch or ambiguous slot ownership after a crash;
- silent loss of candidate writes after automatic rollback;
- resurrection of revoked credentials, callers, or grants after PITR;
- replay of post-recovery client journals, idempotency keys, or old cursors;
- reuse or rebinding of a Page ID whose permanent reservation was lost; and
- a restored audit history that appears continuous while external events from
  the abandoned interval are missing.

Digest verification and SQLite integrity checks are complementary. The former
answers whether bytes match the trusted manifest; the latter answers whether
SQLite can validate structural database invariants. Neither proves that the
snapshot is recent, authorized, free of malicious but valid rows, or safe to
activate.

Administrative erasure across retained backups remains an open public design
question. No retention statement in an implementation may imply that immutable
backups override legal or operator erasure requirements.

## Acceptance tests

Acceptance requires public, synthetic, reproducible evidence for all applicable
items below.

### Backup bundle

- Create backups from both rollback-journal and WAL databases while concurrent
  commits occur; each accepted bundle is a consistent SQLite snapshot.
- Prove that direct source/destination identity and aliases through links or
  platform path indirections fail before a destination is modified.
- Inject cancellation, busy exhaustion, full-disk behavior, short writes, and
  process termination at every publication step. No incomplete directory is
  accepted as a final bundle.
- Reject an existing destination, missing or early manifest, truncated
  database, wrong digest, unknown or duplicate manifest fields, non-canonical
  JSON, non-finite numbers, malformed timestamps, and oversized inputs.
- Demonstrate that manifest replacement can defeat a bare digest and that an
  independently authenticated catalog detects replacement and replay before
  production trust is claimed.
- Confirm restrictive permissions at staging and publication time and prove
  that logs and receipts contain no content, credential material, or private
  infrastructure values.

### Validation and restore

- Exercise integrity and foreign-key failures plus each Page/Revision/Source,
  identifier/counter, auth/audit, and idempotency semantic invariant.
- Reject the correct Alembic version paired with missing or changed tables,
  indexes, constraints, or critical triggers.
- Restore only to a new empty slot, validate the materialized destination, and
  prove that a failure never changes the source, live slot, or backup bundle.
- Verify restore on every supported operating system and in the production OCI
  filesystem/UID model using only synthetic data.
- Prove that exact-schema restore does not run a migration and that upgrade
  migrations run only after a validated restore to the candidate slot.

### Upgrade and rollback

- Block migration when the exact pre-migration backup receipt is absent,
  invalid, unauthenticated where required, or names different source bytes.
- Inject failures before and after every state transition and recover without
  dual writers or loss of the old pair.
- Cold-start the old image with only its old slot and the candidate image with
  only its candidate slot; never use the crossed pairings.
- Demonstrate a pre-write candidate failure that safely returns to the old pair.
- Demonstrate that the first durable candidate write changes the state so every
  automatic rollback path fails closed.
- Prove controller restart behavior from durable receipts and slot ownership,
  including an ambiguous-state test that requires review.

### Point-in-time recovery

- Restore a snapshot predating caller creation, credential rotation and
  revocation, grant changes, idempotent mutations, Page-ID collisions, and
  audit events.
- Prove all restored credentials/callers/grants and both old and newer operation
  journals remain quarantined; rotate cursor signing material before any read
  traffic.
- Append and retain an external discontinuity record while the database is
  rolled backward.
- Prove that post-snapshot Page IDs, collision ordinals, and idempotency keys
  cannot be reused or rebound.
- Keep the restored slot non-serving whenever anti-reuse evidence is missing or
  any recovery interval is ambiguous.

The PITR test group cannot pass until its missing public decisions and durable
state have been implemented. It is intentionally not waived for alpha
real-data readiness.

## Alternatives considered

### Stop the service and copy files

A fully quiescent database can be copied safely when all journal state is kept
together. This is a valid operator technique under a separately proved stop and
filesystem contract, but it is not the default live-backup API. Failure to
prove quiescence or pair the WAL/hot journal can corrupt the copy.

### `VACUUM INTO`

SQLite documents `VACUUM INTO` as another consistent live-backup mechanism. It
produces a compact file and purges deleted free-page content, but uses more CPU,
cannot run from an open transaction on its connection, may change unkeyed
rowids, and an interrupted output may be incomplete. The incremental online
backup API better matches the alpha's live-copy and explicit validation path.
`VACUUM INTO` remains a possible future storage transformation that creates a
new bundle rather than rewriting one.

### SQL dump or application export

A SQL dump is inspectable and an application export can be portable across
versions, but neither automatically preserves every operational invariant,
SQLite schema object, binary value, security record, or idempotent response.
They require independent round-trip contracts and remain complementary tools.

### In-place migration with image-only rollback

This uses less storage but makes rollback depend on every migration being
backward-compatible after partial failure. The current migration-on-start and
image rollback shape does not by itself prove database recovery. The proposal
rejects it for real-data readiness in favor of paired data slots.

### Filesystem or volume snapshot

A storage snapshot can be fast and space-efficient, but only when its
application quiescence, SQLite/WAL coherence, durability, access control, and
restore semantics are proved for each backend. It may become another backup
adapter; it does not weaken the bundle or validation contract.

### Continuous WAL archival

Continuous archival could reduce data loss, but recovery to an older point has
the credential, audit, cursor, identifier, and idempotency hazards described
above. It remains deferred until the PITR security state and anti-reuse design
are accepted.

## Dependencies and implementation order

```text
public acceptance of this proposal
  -> bounded backup/manifest/validation core
    -> local operator backup and exact-schema restore commands
    -> OCI UID/filesystem restore drill
    -> authenticated backup receipt and pre-migration deployment gate
      -> fresh data-slot migration and validation
      -> paired image-and-slot cutover controller
      -> real-data deployment readiness review

public PITR quarantine decision
  -> external discontinuity record
  -> credential/caller/journal/cursor quarantine implementation
  -> accepted Page-ID/collision/idempotency anti-reuse mechanism
    -> destructive and adversarial PITR drill
    -> separate PITR activation decision
```

Prototype code may be developed behind these gates, but downstream components
must not claim that an upstream Proposed decision is accepted. Backup storage,
release publication, secrets, and private deployment remain serialized operator
responsibilities.

## Open questions

Acceptance of the common backup and lossless-upgrade path still requires
answers to these questions:

- Which canonical manifest schema and validation-contract version become the
  first supported wire format?
- Which authenticated catalog, MAC, or signature mechanism is portable enough
  for backups stored outside a trusted local boundary, and how is replay
  prevented?
- What retention, rotation, secure-erasure, and administrative-erasure policy
  applies to immutable bundles?
- What maximum database size, backup duration, busy retry budget, free-space
  reserve, and validation resource budget are supported?
- Which cross-platform filesystem operations and directory-sync guarantees are
  required for bundle publication and data-slot switching?
- How does the controller prove write quiescence or final synchronization while
  producing the cutover copy?
- What durable event marks the candidate's first accepted write, and how does a
  restarted controller verify it without trusting mutable local process state?
- Which derived projections are validated byte-for-byte, rebuilt before
  readiness, or deliberately unavailable after restore?
- How are backups found and erased after an exceptional administrative erasure?

PITR additionally requires separate accepted answers:

- How are restored credentials, callers, grants, and bootstrap authority
  invalidated without relying on the rolled-back database alone?
- Where does the external discontinuity record live, who authorizes it, and how
  is its authenticity and monotonicity protected?
- Which Page-ID reservation and collision-counter high-water ledger prevents
  reuse across an abandoned future timeline?
- How are durable idempotency keys and operation journals reconciled without
  replaying or reassigning an accepted operation?
- What recovery-point objective is supportable once those safety mechanisms
  exist?

Until the common questions are accepted and tested, whole-database tooling is
experimental. Until every PITR question is accepted and tested, an older
snapshot remains quarantine-only.

## Public sources

- [SQLite Backup API](https://sqlite.org/backup.html)
- [SQLite online backup API reference](https://sqlite.org/c3ref/backup_finish.html)
- [Python 3.13 `sqlite3` reference](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)
- [SQLite: How To Corrupt An SQLite Database File](https://sqlite.org/howtocorrupt.html)
- [SQLite write-ahead logging](https://sqlite.org/wal.html)
- [SQLite `VACUUM`](https://sqlite.org/lang_vacuum.html#vacuum-with-an-into-clause)
