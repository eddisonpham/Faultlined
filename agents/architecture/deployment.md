# Deployment

Tiers respect [../spec/environment.md](../spec/environment.md): Windows 11 laptop, Git Bash, Python 3.14, **no Docker**,
one RTX 5060 8 GB GPU, ~228 GB free NVMe. Secrets per ADR [0002](../decisions/0002-secrets-handling.md):
environment variables only.

## Tiers

| Tier | Shape | Purpose |
|---|---|---|
| **Local development** | Host processes: Postgres (local install/service), `de worker` pool, `de api` (or `de dev` running both), artifacts under `./var/` | Day-to-day development, tests, benchmarks. Everything runnable under Git Bash. |
| **Production-like local** | Same host, but: Postgres as a Windows service, API + workers started by Windows Task Scheduler with "restart on failure", dedicated artifact volume, and a nightly backup task | Closest-to-real operation: restart drills, runbooks, load/soak tests (definition-of-done stages 3 & 5). **Documented, not shipped**: see [What is manual](#what-is-manual-in-this-tier) below and [docs/runbooks/](../../docs/runbooks/worker-operations.md) |
| **Optional cloud** | Deferred. Trigger: multi-machine or S3-scale need (technology-matrix) | A new ADR must define it (OSMO-style backends are the reference pattern, #31) |

Containerization is **deferred**, not banned: a Dockerfile may be added as an *optional* delivery artifact when a
container runtime exists (ADR [0012](../decisions/0012-host-based-development.md)), never as a dev prerequisite.

## Processes

| Process | Command | Notes |
|---|---|---|
| API | `just api` (`de api`) | Localhost bind is the v1 security boundary (ADR 0009). Address comes from `DE_API_HOST`/`DE_API_PORT`, not from flags — there are no bind flags |
| Worker | `just worker` (`de worker`), `--once` to process at most one job | One process, one job at a time. Run N of them for concurrency; `DE_WORKER_SLOTS` is read by settings but slot leasing and GPU slots are **not** implemented (compute-orchestration.md) |
| Dev all-in-one | `just run` (`de dev`) | API + a worker child process |
| Migrate | `just migrate` (`de migrate`) | Applies pending catalog migrations and records them (ADR [0028](../decisions/0028-versioned-catalog-migrations.md)); `--status` reports applied/pending and exits nonzero when the database is newer than the code |
| GC / maintenance | `just gc` (`de gc`) | **Stub**: prints that no GC work is implemented. Orphan blobs, stale tmp, and metric retention are stage-5 work |
| Doctor | `just doctor` (`de doctor`) | Postgres reachable and catalog schema initializes. Disk headroom, GPU visibility, and lockfile-hash checks are **not** implemented |

**Monitoring is the worker's job, not a timer of its own.** `_worker_loop` evaluates one monitoring
window every 60 s under a catalog-held advisory lease, so a pool of N workers produces one tick per
interval rather than one each (ADR [0031](../decisions/0031-monitor-scheduling.md)). A deployment with
no worker therefore has no monitor: `de api` alone never opens an incident. `POST
/api/v1/monitoring/tick` remains an on-demand probe, not the schedule.

## Configuration

Application settings read process environment variables only (per project secrets policy); the `just` task runner loads a
local `.env` into recipe process environments, or operators may export variables in their shell. Defaults apply when unset.
Keys (`.env.example` holds placeholders only):

| Var | Meaning | Default |
|---|---|---|
| `DE_DATABASE_URL` | Postgres DSN (secret-bearing — env only, never logged) | `postgresql://localhost:5432/data_engine` |
| `DE_ARTIFACT_ROOT` | Artifact store path | `./var/artifacts` |
| `DE_LOG_LEVEL` / `DE_LOG_FORMAT` | Logging | `INFO` / `json` |
| `DE_WORKER_SLOTS` | `cpu=N,gpu=N` | auto |
| `DE_API_HOST` / `DE_API_PORT` | Bind address | `127.0.0.1:8000` |
| `HF_KEY` | Hugging Face token (dataset pulls; optional) | unset — never required for core flows |

Rules: secrets never appear in CLI args, logs, or the catalog; `gitignore` covers `.env*` (except `.env.example`);
the hygiene check scans for key patterns (already enforced in CI).

## What is manual in this tier

Stated plainly, because a tier that reads as automated and is not is worse than one that
does not exist. Nothing in this repository installs a service, schedules a task, or
supervises a process; the OS does, and the runbooks say so.

| Concern | Automated | Manual |
|---|---|---|
| Schema creation | `initialize_schema` on every start (idempotent; records baseline migration 0001, ADR [0028](../decisions/0028-versioned-catalog-migrations.md)) | Running `de migrate` when a versioned change ships |
| API + worker lifecycle | Windows Task Scheduler, "restart on failure" | Creating the two tasks once per machine |
| Nightly catalog backup | — | One scheduled task running the block in [backup-and-restore.md](../../docs/runbooks/backup-and-restore.md) |
| Artifact/export backup | — | `cp -a` of two immutable trees; re-hash to verify |
| Off-machine copy of backups | — | Manual, and it is the one that matters |
| Log rotation | — | Manual; JSON logs go to stdout and are whatever you redirected them to |
| Garbage collection | — | **`de gc` is not implemented** and says so when run. Nothing prunes artifacts, metrics, or old jobs yet |
| GPU slot leasing | — | Not implemented; the worker is single-process (worker-ops runbook) |

`docs/runbooks/backup-and-restore.md` and `docs/runbooks/worker-operations.md` are the
operational contract for this tier. Stage 5 ([definition-of-done](../spec/definition-of-done.md))
owns the restore drill and fault injection; the runbooks are written now so those drills
have something to follow.

## Data management

- **Backup:** catalog = `pg_dump --format=custom`; artifacts and exports are immutable → file-level copy is safe; manifests
  make backup verification cheap (re-hash). Full procedure, including how to find `pg_dump` on Windows and how to
  verify a dump is loadable rather than merely present: [backup-and-restore.md](../../docs/runbooks/backup-and-restore.md).
- **Restore drill:** procedure written and linked above; **exercising it** is part of Production Hardening
  (definition-of-done stage 5) and has not been run yet.
- **Upgrade:** schema migrations are forward-only and numbered ([ADR 0028](../decisions/0028-versioned-catalog-migrations.md)).
  `initialize_schema` still runs on every process start and is the baseline (recorded as migration `0001`); `de migrate`
  applies anything after it, once each, recorded in `schema_migrations`. `de migrate --status` proves a database is
  neither behind (pending) nor ahead of the code (unknown versions). Artifact layout changes require a new
  `schema_version` in manifests (storage.md §3).

## Windows notes

- All scripts POSIX-compatible (Git Bash); forward-slash paths; no Linux-only syscalls (`os.replace`, `fcntl`-free).
- Path length / permission caveats documented in the runbooks; artifacts use short hash prefixes (storage.md §2).
- Python 3.14 wheel availability verified at tooling time (ADR 0004 note); lockfile pins everything (NFR-011).
