# Changelog

All notable changes to PatchouliLib will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project intends to use [Semantic Versioning](https://semver.org/) once
it publishes a supported implementation.

## [Unreleased]

### Added

- Initial public product and architecture documentation.
- Community governance, contribution, support, and security policies.
- Documentation validation workflow and contribution templates.
- Python/FastAPI service bootstrap with liveness and readiness endpoints.
- SQLite/FTS5 validation and reversible Alembic migration plumbing.
- Cross-platform source, test, migration, documentation, and container checks.
- GHCR image publishing, provenance, releases, and an operator-initiated
  private update helper.

### Changed

- GitHub Actions no longer stores private SSH deployment settings or connects
  to private targets; an operator must log in separately and select an exact
  published image digest.
