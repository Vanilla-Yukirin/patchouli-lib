# Contributing to PatchouliLib

Thank you for helping shape PatchouliLib. The project is currently in its
design stage, so a clear use case or failure mode is often more useful than a
large implementation.

## Before you start

1. Read the [public design map](docs/README.md) and [open questions](docs/08-open-questions.md).
2. Search existing issues and discussions before creating a new proposal.
3. Open a design proposal before making a change that affects the domain model,
   persistence invariants, security model, or public API.

Small documentation fixes can go directly to a pull request.

## Public-data boundary

Only use synthetic or deliberately public examples. Do not submit:

- credentials, tokens, private keys, cookies, or connection strings;
- personal hostnames, usernames, email addresses, filesystem paths, or IPs;
- private conversation archives, production logs, database dumps, or deployment
  topology;
- generated content that has not been reviewed by a human contributor.

If a real incident motivates a proposal, replace identifying details with a
minimal synthetic reproduction.

## Documentation setup

The documentation checks require a current Node.js LTS release.

```sh
npm ci
npm run lint:docs
```

## Pull requests

- Keep one coherent change per pull request.
- Explain what changed, why it changed, and which design invariant it affects.
- Update linked design documents and the changelog when behavior changes.
- Include validation output or a reproducible validation command.
- Use short, imperative commit subjects. Conventional Commits are encouraged.
- Confirm that the diff contains no private or operator-specific information.

Maintainers may ask for a proposal to be split when consensus and implementation
need separate review.
