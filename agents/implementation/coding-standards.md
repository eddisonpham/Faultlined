# Coding Standards

Tool names and commands are filled in by phase 04 (see the ADRs for the chosen stack).

## Principles
- Smallest clean abstraction that solves the problem. No speculative generality, no unused hooks.
- Modules have one reason to change. Dependencies point inward: domain logic never imports frameworks/IO.
- Explicit over clever. Prefer plain functions and typed data over deep class hierarchies.
- No dead code, no duplicated logic, no commented-out code.
- Comments/docstrings: only for non-obvious intent, invariants, or trade-offs. Never restate the code.

## Correctness and robustness
- Validate at boundaries (API input, file/episode ingestion, config); trust internal invariants.
- Errors are typed and carry context (job ID, episode ID). No bare `except`, no silent fallbacks.
- Every external call has a timeout. Retried operations must be idempotent.
- Config is typed, validated at startup, and comes from files + environment variables. No magic constants inline.
- Determinism: seeds are explicit and recorded whenever randomness affects results.

## Secrets
- Read from environment variables only. Never log them, never put them in errors, fixtures, docs, or commit history.
- `.env` is gitignored; `.env.example` has empty placeholders.

## Dependencies
- Adding a dependency requires a line in `research/technology-matrix.md` or an ADR: what it does, why not stdlib/existing dep.
- Pin versions; commit lockfiles.

## Git
- Conventional commits; one logical change per commit; docs updated in the same commit.
- Before commit: format, lint, type-check, tests, `python scripts/check_repo_hygiene.py`.
- PR checklist: `.github/pull_request_template.md`.

## Language policy
- Default language and any use of C++ / Rust must be justified by an ADR that names the measured or structural reason.

## Commands
<!-- TODO(phase 04): setup / fmt / lint / typecheck / test / bench / run -->
