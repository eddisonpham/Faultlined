# Faultlined

A local-first robotics ML data platform: ingest robot episodes, validate them, and build **reproducible,
content-addressed datasets** with end-to-end lineage. Models are workloads the platform executes — not the product.
See [agents/spec/problem.md](agents/spec/problem.md) and [agents/architecture/overview.md](agents/architecture/overview.md).

**What works today, stated plainly.** **Both raw formats the storage design chose are readable**:
LeRobot (v2.1 and v3.0) and MCAP. What is real end to end: ingest → validate → **build**, where a
build's identity is the hash of its own manifest, so the same selection under the same policy and
commit always produces the same dataset address — determinism is structural, not a convention.

Three limits worth knowing before you rely on it, all visible on the episode itself rather than
buried in a caveat:

- Materialising a build as LeRobot dataset files on disk is **not** implemented; the manifest and its
  lineage are.
- The MCAP reader interprets `json`-encoded payloads only. CDR and protobuf channels are counted,
  timed and named, but contribute no numbers — a guessed struct layout would put wrong values in the
  catalog.
- **There is no performance claim.** MCAP ingest measures **6.21 MiB/s** against a provisional
  50 MB/s target ([EXP-0004](agents/experiments/0004-mcap-ingest-baseline.md)). The container itself
  reads at 92 MiB/s; 51% of the cost is one function of ours, so this is a known and located gap
  rather than a mystery.

## Requirements

- Python 3.14, [uv](https://docs.astral.sh/uv/), and [just](https://just.systems/) (task runner).
- PostgreSQL 17+ running locally. The service is an explicit architecture choice (ADR 0005); Docker is optional and not
  required. On Windows, install PostgreSQL with its installer and keep the server bound to localhost.
- Git Bash on Windows is supported. GPU is optional; the core pipeline and tests run CPU-only.

## Quickstart

1. Clone the repository. On Windows, install [Git for Windows](https://gitforwindows.org/): the justfile runs
   recipes through Git Bash, and a PowerShell prompt without it fails with `could not find the shell`.
   You can then use `just` from PowerShell or Git Bash.
2. Start an isolated local PostgreSQL for tests. This creates a cluster in gitignored `var/pgdata` on port
   `55432`; it does not touch any system PostgreSQL install and needs no administrator rights and no password.

   ```bash
   just pg-up
   ```

   To use an existing PostgreSQL server instead, skip this and put your own DSN in `.env` (step 3).
3. Create local config and point it at that server:

   ```bash
   cp .env.example .env
   # For the isolated cluster, set:
   #   DE_DATABASE_URL=postgresql://data_engine@127.0.0.1:55432/data_engine
   # That DSN has no password. For your own server, edit .env locally;
   # never paste credentials into chat or commit them.
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
Postgres instance are marked and need `DE_DATABASE_URL` set in your local `.env` or shell; with `just pg-up` and the
DSN above, the whole suite runs with nothing skipped. Use `just pg-status` to check the cluster and `just pg-down` to
stop it.

The current repository stage is **MVP complete** — all 27 criteria in [the definition of done](agents/spec/definition-of-done.md) are checked with evidence, and the production-baseline stage is next. `just run` requires a configured PostgreSQL database. `just bench` runs every workload and compares the synthetic one against its committed baseline; `just bench --workload mcap-ingest` measures real-format ingest against [EXP-0004](agents/experiments/0004-mcap-ingest-baseline.md). Scope and limitations are recorded in [the handoff](agents/HANDOFF.md). Host-based development with optional containers is recorded in [ADR 0012](agents/decisions/0012-host-based-development.md).

## Try it

`just run` starts the API on `http://127.0.0.1:8000` and a worker beside it. Open `http://127.0.0.1:8000/ui` for the operator UI — Status, Jobs, Episodes, Incidents, Metrics (server-rendered, polling every 3-5 s).

**To put data in:** open `http://127.0.0.1:8000/ui`. On a system that has never run a job, the
Status page leads with an **Ingest data** panel — name a task and robot, or point at a LeRobot
dataset directory or an MCAP file, and press *queue ingest job*. It is a plain form, so it works with JavaScript
disabled, and the job page it redirects to shows the queue, the result, and the episode it
registered. The panel disappears once there is anything to look at.

Ingest can also be submitted from the command line, which is what the automated tests use:

```bash
# health check
curl -s http://127.0.0.1:8000/api/v1/health

# submit an ingest job (the worker picks it up within a second)
JOB=$(curl -s -X POST http://127.0.0.1:8000/api/v1/jobs   -H 'Content-Type: application/json'   -H 'Idempotency-Key: demo-1'   -d '{"type":"ingest","payload":{"episode":{"task":"pick_place","robot":"arm",
       "timestamps":[0.0,0.1,0.2],"observations":[[0.0],[1.0],[2.0]],
       "actions":[[0.1],[0.2],[0.3]]}}}')
echo "$JOB"

# watch it finish
curl -s http://127.0.0.1:8000/api/v1/jobs/<id>

# the registered episode, with lineage back to the job
curl -s http://127.0.0.1:8000/api/v1/episodes/<episode_id>
```

The response carries `X-Correlation-Id` and `X-Idempotent-Replay`. Re-sending the same
`Idempotency-Key` **with the same payload** replays the original job (`x-idempotent-replay: true`);
re-sending it with a **different** payload is a `409`.

Tests use a separate `data_engine_test` database, so you can leave `just run` going while running `just test`
without the dev worker claiming the jobs the suite submitted.

Written artifacts land under `var/artifacts/blobs/`, and runtime metrics are appended to
`var/metrics/runtime.jsonl` (queue depth, queue time, run time, stage duration, ingest counters).
The machine-readable contract is at `http://127.0.0.1:8000/openapi.json`; there is deliberately no
rendered API browser at `/docs` (ADR 0021 — the browser surface is the operator UI, and a generated
contract viewer does not belong in an operator's primary navigation).

## Task commands

```bash
just setup       # install locked deps
just pg-up        # isolated local PostgreSQL for tests (var/pgdata, port 55432)
just pg-status    # is that cluster running?
just pg-down      # stop it
just fmt         # format + safe lint fixes
just lint        # check without writing
just typecheck   # strict mypy
just test        # fast tests + coverage gate
just test-all    # include slow and GPU-marked tests
just bench       # synthetic harness + comparison against the committed baseline
just run         # local API + worker together (Ctrl+C to stop)
just stop        # stop the server from `just run` (safe: refuses foreign processes)
just api         # HTTP API only
just worker      # ingest worker only (-- --once to process a single job)
just doctor      # check database connectivity and catalog schema
just hygiene     # relative links, ADRs, secrets, required structure
just reset --yes # wipe the dev catalog, artifacts, and metrics (refuses *_test databases)
python scripts/verify_e2e.py  # 145-check end-to-end run against `just run`; needs an empty catalog
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
