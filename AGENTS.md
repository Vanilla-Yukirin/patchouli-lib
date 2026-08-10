# AI-assisted contribution guide

This repository welcomes carefully reviewed AI-assisted contributions.

- Treat the tracked repository as public. Never copy private notes, chat logs,
  operator identifiers, secrets, hostnames, or deployment topology into a
  tracked file, commit, issue, or pull request.
- Use `docs/` as the public design source of truth. Historical or local-only
  material is not automatically a public requirement.
- Preserve the distinction between accepted decisions and open questions.
- Keep retrieval providers and deployment targets configurable unless a public
  design proposal establishes a portable default.
- Before proposing a commit, inspect the complete staged diff and run the
  documented validation commands.
- Human contributors remain responsible for correctness, licensing, privacy,
  and the final submitted text.

## Parallel agent work

- Keep one coordinating agent or contributor responsible for the active plan,
  integration branch, cross-cutting decisions, and final acceptance.
- Give every delegated task a bounded objective, explicit file ownership,
  required reading, constraints, validation commands, and expected handoff.
- Use one writer per path. Agents working in a shared worktree must not switch
  branches, stage, commit, push, merge, or deploy unless the coordinator assigns
  that responsibility explicitly.
- Prefer read-only agents for independent review, design comparison, and test-gap
  analysis. Stop and notify the coordinator if discovered work overlaps another
  task's files or changes a public contract.
- A handoff must name changed files, validation performed, decisions made,
  unresolved risks, and the public or private status of every source used.
- Keep secret management, release publication, and private deployment serialized
  under one authorized integrator.

See [the agent contribution workflow](docs/agent-contribution-workflow.md) for
task briefs, shared-worktree rules, integration gates, and handoff requirements.
