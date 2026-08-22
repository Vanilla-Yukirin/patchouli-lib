# Agent contribution workflow

This workflow keeps parallel human and AI-assisted work reviewable without
turning local context into an undocumented public requirement.

## Sources of truth

Use sources in this order:

1. tracked code, tests, and accepted ADRs define implemented behavior;
2. `docs/` defines the public design direction and decision status;
3. `docs/08-open-questions.md` identifies choices that implementations must not
   silently freeze;
4. issues and pull requests propose changes but are not accepted facts until
   merged.

Private notes, conversations, deployment settings, and local archives are not
public requirements. A contribution derived from private context must be
rewritten as a target-neutral public problem statement and reviewed against the
public design before it enters a tracked file.

## Roles

One coordinator owns integration for an active change. The coordinator:

- maintains the plan and assigns non-overlapping scopes;
- resolves cross-cutting decisions and file conflicts;
- owns branch changes, staging, commits, pushes, merges, releases, and deployment;
- inspects the complete integrated diff and accepts the final result.

Workers receive bounded implementation or research tasks. Reviewers are
read-only by default and report findings without modifying the worktree. A
worker must stop and report when a discovered change exceeds its assigned files
or alters a public contract.

## Delegation brief

Every delegated task should include:

```text
Objective:
Base commit:
In scope:
Owned files:
Read-only files:
Required reading:
Constraints and decisions already accepted:
Dependencies:
Validation commands:
Expected handoff:
Explicitly out of scope:
```

The brief should be small enough that completion can be judged without relying
on the original conversation. Do not include credentials or operator-specific
infrastructure in a public task brief.

## Shared-worktree safety

- Assign one writer per file or directory for the duration of a task.
- Workers do not switch branches or change Git state unless explicitly assigned.
- Record the base commit and inspect `git status` before editing and before
  handoff. Existing unrelated changes belong to another contributor and must be
  preserved.
- If an owned file changes after the recorded base, stop and report the drift.
  Do not overwrite, revert, or resolve the overlap without reassignment.
- Do not run formatters or mechanical rewrites outside owned files.
- If scopes overlap, pause one writer and let the coordinator integrate the
  first handoff before reassigning the second.
- Workflow files, dependency manifests and lock files, migration chains, design
  indexes, open-question ledgers, and changelogs belong to the coordinator by
  default because they have high integration cost.
- Infrastructure changes and external publication remain serialized even when
  source analysis and testing run in parallel.

## Handoff contract

A worker handoff must report:

- outcome and exact files changed;
- base and head commits used for the work;
- decisions made and the evidence used;
- commands run and their results;
- checks not run and the reason;
- remaining failures, uncertainty, or follow-up work;
- dependencies, detected drift, and the suggested next owner;
- whether any source was private and how it was prevented from entering the
  public diff.

The coordinator verifies the handoff against the actual worktree. A claim of
completion is not a substitute for inspecting the diff and rerunning relevant
checks. Reports should distinguish worker validation, integrated-tree
validation, required CI, and deployment verification.

## Design-state gate

Work touching an area marked **Partly open**, **Experimental direction**, or
**Active** may produce a proposal, prototype, or testable experiment. It must
not silently establish a public interface or compatibility promise. Changes to
entities, persistence invariants, authorization, or public APIs require the
design-proposal process described in `CONTRIBUTING.md`.

## Integration gate

Before a pull request or merge, the coordinator must:

1. inspect the complete staged or proposed diff;
2. stage explicit public paths and confirm them against the ownership list;
3. scan for credentials, personal identifiers, target hosts, paths, and topology;
4. run `python scripts/validate.py`;
5. run `python scripts/validate.py --container` for delivery or container changes;
6. record any skipped check and its reason in the pull request;
7. wait for required independent CI checks before merge.

GitHub workflows do not connect to private runtimes. A private update is a
separate, operator-initiated action using local configuration after an exact
published image digest has passed the required gates. Agents must not copy
private runtime values into source, logs, issues, pull requests, or handoff
examples, and must not perform the update without explicit deployment
authorization.
