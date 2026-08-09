# Page revisions, history, and recovery

## Invariants

- A normal edit creates a new immutable Revision.
- A Page has one current Revision pointer.
- Default reads and searches use only the current Revision.
- Historical Revisions are available only through explicit history operations.
- Revision creation and the current-pointer update occur in one transaction.

## Revision numbers

Revision numbers increase within a Page: `1`, `2`, `3`, and so on. They are
human-friendly local sequence numbers, not global identifiers or timestamps.

## Recovery

Recovery creates a new Revision from an earlier body rather than moving the
current pointer backward.

```text
Revision 2 (historical content)
    |
    | restore with an explanatory message
    v
Revision 6 (new current revision)
```

This keeps history linear and records that a restore occurred.

## Concurrent writes

The service serializes writes through one API and never discards a committed
Revision. A later accepted write can become current while earlier writes remain
in history.

The API should support an optional expected-current value, such as a Revision
number or entity tag. A mismatch can produce a conflict response instead of
silently accepting a write based on stale input. The exact default is tracked
in [08-open-questions.md](08-open-questions.md).

## Moving content

Moving a Page to another Book changes Page metadata; it does not create a new
copy or rewrite Revision bodies. The move itself is an audit event.

## Soft deletion

Normal deletion sets a tombstone such as `deleted_at`. Deleted Pages are hidden
from default reads and search but can be restored by an authorized actor.

## Administrative erasure

Immutable history cannot mean that leaked credentials, illegal content, or
personal data are impossible to remove. A future implementation must provide a
rare, strongly authorized erasure path with impact preview, audit metadata, and
backup guidance. Erasure semantics must be settled before a supported release.

## Backup and export

Application-level export and tested restore are required. Version-control tools
may store exported text snapshots, but they are not the database transaction
engine and must not be presented as the only disaster-recovery mechanism.
