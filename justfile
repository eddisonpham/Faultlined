set dotenv-load := true

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

# Lint and format check, no writes
lint:
    uv run --all-extras ruff check .
    uv run --all-extras ruff format --check .

# Strict type-check of the engine and the experiments package
typecheck:
    uv run --all-extras mypy
    uv run --all-extras mypy experiments

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

# Stage 2.5 task-string clustering experiments
# Subcommands: gold | centroids | views | factorial | audit | heldout | all
cluster *ARGS:
    uv run --all-extras python -m experiments.clustering.runner {{ARGS}}

# Run the platform locally (API + worker in one process group)
run:
    uv run --all-extras python scripts/check_port.py
    uv run --all-extras de dev

# Stop the dev server listening on the API port
stop:
    uv run --all-extras python scripts/stop_server.py

# Run only the HTTP API
api:
    uv run --all-extras de api

# Run only the ingest worker (add --once to process at most one job)
worker *ARGS:
    uv run --all-extras de worker {{ARGS}}

# Apply pending catalog migrations (ADR 0028); --status reports without changing anything
migrate *ARGS:
    uv run --all-extras de migrate {{ARGS}}

# Check database connectivity and catalog schema
doctor:
    uv run --all-extras de doctor

# Garbage collection (not implemented in the vertical slice)
gc:
    uv run --all-extras de gc

# Empty the development catalog, its artifact blobs, and the metrics log (requires --yes)
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

# Measure the rendered UI in a real browser (Node 22+, Chrome, a server already running)
ui-audit *ARGS:
    node scripts/ui_audit.mjs {{ARGS}}

# Every gate, in one command
ci: lint typecheck test hygiene api-contract
