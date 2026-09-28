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
    uv run ruff format .
    uv run ruff check --fix .

# Lint + format check (no writes)
lint:
    uv run ruff check .
    uv run ruff format --check .

# Strict type-check of all package modules
typecheck:
    uv run mypy

# Fast tests + coverage gate (unit/integration/contract/e2e; excludes slow+gpu)
test:
    uv run pytest -m "not slow and not gpu" --cov --cov-report=term-missing

# All tests including slow and gpu-marked ones
test-all:
    uv run pytest --cov

# Run benchmarks and compare against committed baselines (phase 06)
bench *ARGS:
    uv run python -m benchmarks.harness {{ARGS}}

# Run the platform locally (API + workers)
run:
    uv run de dev

# Repository hygiene (structure, links, ADRs, secret patterns)
hygiene:
    python scripts/check_repo_hygiene.py

# Everything CI runs
ci: lint typecheck test hygiene
