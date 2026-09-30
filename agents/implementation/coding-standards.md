# Coding Standards

Tooling follows ADR [0004](../decisions/0004-language-and-toolchain.md): Python 3.14, uv, ruff, mypy, pytest + pytest-cov; task runner is `just`.

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
- Config is typed, validated at startup, and comes from environment variables; `just` may load a gitignored local `.env` into the process environment. No magic constants inline.
- Determinism: seeds are explicit and recorded whenever randomness affects results.

## Secrets
- Read from environment variables only. Never log them, never put them in errors, fixtures, docs, or commit history.
- `.env` is gitignored; `.env.example` has placeholders only. Agents never open `.env`; application code reads environment variables only.

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

Run from the repository root (`just` is the single task entry point; `uv.lock` pins dependencies):

| Goal | Command |
|---|---|
| Setup | `just setup` |
| Format | `just fmt` |
| Lint | `just lint` |
| Typecheck (package, strict) | `just typecheck` |
| Fast tests + coverage | `just test` |
| Full tests (slow/GPU included) | `just test-all` |
| Benchmark (compare baseline) | `just bench` |
| Create/update baseline (one-time, deliberate) | `just bench --write-baseline --baseline benchmarks/baselines/<name>.json` |
| Run locally | `just run` |
| Stop the local server | `just stop` |
| Hygiene | `just hygiene` |
| All CI gates | `just ci` |

Coverage: 70% floor, branch coverage, ratchet-up only per [ADR 0011](../decisions/0011-coverage-gate.md).
Integration tests requiring Postgres use `DE_DATABASE_URL`; no credentials are committed. Docker is intentionally optional
(ADRs 0005/0012 + [deployment.md](../architecture/deployment.md)); setup does not require a container runtime.
