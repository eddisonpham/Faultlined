# Task runner (just is the single documented entry point; see agents/implementation/coding-standards.md).
# Prerequisites: Python 3.14, uv, just, PostgreSQL (see README Quickstart).
# just imports local .env into each recipe's process environment; the application reads environment variables only.
set dotenv-load := true

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

# Run benchmarks and compare against committed baselines (phase 06)
bench *ARGS:
    uv run --all-extras python -m benchmarks.harness {{ARGS}}

# Run the platform locally (API + worker in one process group)
run:
    uv run --all-extras de dev

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

# Everything CI runs
ci: lint typecheck test hygiene
