# Faultlined

A local-first robotics ML data platform: ingest MCAP / LeRobot episodes, validate and index them, then build reproducible,
versioned LeRobot datasets with end-to-end lineage. Models are workloads the platform executes — not the product.
See [agents/spec/problem.md](agents/spec/problem.md) and [agents/architecture/overview.md](agents/architecture/overview.md).

## Requirements

- Python 3.14, [uv](https://docs.astral.sh/uv/), and [just](https://just.systems/) (task runner).
- PostgreSQL 17+ running locally. The service is an explicit architecture choice (ADR 0005); Docker is optional and not
  required. On Windows, install PostgreSQL with its installer and keep the server bound to localhost.
- Git Bash on Windows is supported. GPU is optional; the core pipeline and tests run CPU-only.

## Quickstart

1. Clone the repository and open Git Bash in its root.
2. Create a local database named `data_engine` in PostgreSQL.
3. Create local config from the placeholder template and set your own database URL/password locally:

   ```bash
   cp .env.example .env
   # Edit .env locally; never paste credentials into chat or commit them.
   ```

4. Install and run the gates:

   ```bash
   just setup
   just fmt
   just lint
   just typecheck
   just test
   just hygiene
   ```

`just` imports a local `.env` into each recipe's process environment; the application itself reads environment variables
only. `just setup` creates `.venv` and syncs the committed `uv.lock` (including optional GPU telemetry, which degrades
without a GPU). `just test` runs the fast suite and enforces the 70% coverage floor. Integration tests requiring a live
Postgres instance are marked and need `DE_DATABASE_URL` set in your local `.env` or shell.

The current repository stage is **scaffolding (not yet accepted)**. A synthetic JSON API/worker/artifact vertical slice and benchmark/telemetry foundations exist; scope and limitations are recorded in [the handoff](agents/HANDOFF.md). `just run` requires a configured PostgreSQL database. `just bench` runs a synthetic local workload but currently cannot complete baseline comparison from a fresh checkout because no baseline has been authorized or committed. Host-based development with optional containers is recorded in [ADR 0012](agents/decisions/0012-host-based-development.md).

## Task commands

```bash
just setup       # install locked deps
just fmt         # format + safe lint fixes
just lint        # check without writing
just typecheck   # strict mypy
just test        # fast tests + coverage gate
just test-all    # include slow and GPU-marked tests
just bench       # synthetic harness + comparison; needs a committed baseline (currently absent)
just run         # local API + worker (requires local PostgreSQL and DE_DATABASE_URL if non-default)
just hygiene     # relative links, ADRs, secrets, required structure
just ci          # lint + typecheck + test + hygiene
```

## Project context

- [CLAUDE.md](CLAUDE.md) — agent instructions and non-negotiables.
- [agents/README.md](agents/README.md) — map of persistent project context.
- [agents/spec/definition-of-done.md](agents/spec/definition-of-done.md) — stage criteria.
- [agents/HANDOFF.md](agents/HANDOFF.md) — current status / next steps.
- Phase prompts are listed in [agents/prompts/README.md](agents/prompts/README.md).

## Secrets and data

Keep credentials in your shell or a local `.env` file (gitignored). `.env.example` contains placeholders only. Never paste
a credential into chat, code, logs, Markdown, or Git history. The hygiene script checks for common key patterns. Raw data,
artifacts, runs, model weights, and `.env` are ignored by Git.

## Workflow

The project followed phases 01–03 (research/problem selection, requirements/technology decisions, architecture), with owner
approval at both checkpoints. Remaining scaffolding phases are 04–07. See [agents/prompts/README.md](agents/prompts/README.md).
