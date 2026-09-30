# Task runner (just is the single documented entry point; see agents/implementation/coding-standards.md).
# Prerequisites: Python 3.14, uv, just, PostgreSQL (see README Quickstart).
# just imports local .env into each recipe's process environment; the application reads environment variables only.
set dotenv-load := true

# just defaults to `sh -cu` on Windows, which resolves via PATH. A PowerShell prompt
# that has Git's `bin` but not `usr\bin` on PATH cannot find it, and every recipe then
# fails with "could not find the shell". Point it at Git Bash explicitly. On a machine
# where Git lives elsewhere, change this path (it is not read from an env var).
set windows-shell := ['C:\Program Files\Git\bin\bash.exe', '-uc']

default:
    @just --list

# Install all dependencies (creates .venv, syncs from uv.lock)
setup:
    uv sync --locked --all-extras

# Format and apply safe lint fixes
fmt:
    uv run --all-extras ruff format .
    uv run --all-extras ruff check --fix .

# Lint + format check (no writes)
lint:
    uv run --all-extras ruff check .
    uv run --all-extras ruff format --check .

# Strict type-check of all package modules
typecheck:
    uv run --all-extras mypy

# Fast tests + coverage gate (unit/integration/contract/e2e; excludes slow+gpu)
test:
    uv run --all-extras pytest -m "not slow and not gpu" --cov --cov-report=term-missing

# All tests including slow and gpu-marked ones
test-all:
    uv run --all-extras pytest --cov

# Targeted run while iterating, without the coverage gate: just t <path> [-k expr]
t *ARGS:
    uv run --all-extras pytest {{ARGS}} -p no:cacheprovider --no-cov -x -q

# Run benchmarks and compare against committed baselines (phase 06)
bench *ARGS:
    uv run --all-extras python -m benchmarks.harness {{ARGS}}

# Run the platform locally (API + worker in one process group)
# Checks the port first: a second `just run` (or a forgotten background server)
# otherwise dies with a bare WinError 10048, which reads like a bug in this
# project rather than "something you started earlier is still listening".
# The check is a script, not a shebang recipe: `#!/usr/bin/env bash` makes `just`
# resolve the interpreter via `cygpath`, which is missing from a PowerShell PATH
# that has Git's `bin` but not `usr\bin` - the same trap `windows-shell` above
# already documents.
run:
    uv run --all-extras python scripts/check_port.py
    uv run --all-extras de dev

# Stop the dev server listening on the API port. The counterpart to `run`:
# a forgotten background server is the single most common way `just run`
# "breaks", and netstat-then-taskkill is a ritual that gets mistyped.
# Refuses to kill a process whose image name does not look like this
# project's own server (see scripts/stop_server.py).
stop:
    uv run --all-extras python scripts/stop_server.py

# Run only the HTTP API
api:
    uv run --all-extras de api

# Run only the ingest worker (add --once to process at most one job)
worker *ARGS:
    uv run --all-extras de worker {{ARGS}}

# Check database connectivity and catalog schema
doctor:
    uv run --all-extras de doctor

# Garbage collection (not implemented in the vertical slice)
gc:
    uv run --all-extras de gc

# Empty the development catalog, its artifact blobs, and the metrics log.
# Refuses a *_test database and refuses to run without --yes: the recipe takes an
# argument so the confirmation is deliberate rather than a habit.
reset *ARGS:
    uv run --all-extras python scripts/reset_data.py {{ARGS}}

# Repository hygiene (structure, links, ADRs, secret patterns)
hygiene:
    python scripts/check_repo_hygiene.py

# Start an isolated local PostgreSQL for tests (gitignored var/pgdata, no admin, no password)
pg-up:
    uv run --all-extras python scripts/dev_postgres.py up

# Stop the isolated local PostgreSQL started by pg-up
pg-down:
    uv run --all-extras python scripts/dev_postgres.py down

# Report whether the isolated local PostgreSQL is running
pg-status:
    uv run --all-extras python scripts/dev_postgres.py status

# Regenerate the committed OpenAPI contract (do this deliberately with an API change)
api-contract-write:
    uv run --all-extras python scripts/openapi_contract.py --write

# Verify the OpenAPI contract has not drifted from the committed copy
api-contract:
    uv run --all-extras python scripts/openapi_contract.py --check

# Measure the rendered UI in a real browser (computed styles and geometry).
# Needs Node 22+ and Chrome/Chromium, and a server already running (`just run`).
# Not part of `ci`: it needs a browser and a live server, so it is a deliberate
# check rather than something that runs on every save.
ui-audit *ARGS:
    node scripts/ui_audit.mjs {{ARGS}}

# Everything CI runs
ci: lint typecheck test hygiene api-contract
