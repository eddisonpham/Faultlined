# Deployment

Tiers respect [../spec/environment.md](../spec/environment.md): Windows 11 laptop, Git Bash, Python 3.14, **no Docker**,
one RTX 5060 8 GB GPU, ~228 GB free NVMe. Secrets per ADR [0002](../decisions/0002-secrets-handling.md):
environment variables only.

## Tiers

| Tier | Shape | Purpose |
|---|---|---|
| **Local development** | Host processes: Postgres (local install/service), `de worker` pool, `de api` (or `de dev` running both), artifacts under `./var/` | Day-to-day development, tests, benchmarks. Everything runnable under Git Bash. |
| **Production-like local** | Same host, but: Postgres as a Windows service, API + workers as supervised background processes (Task Scheduler / `nssm`-style wrappers documented, not required), dedicated artifact volume, log rotation, nightly GC + backup of the catalog | Closest-to-real operation: restart drills, runbooks, load/soak tests (definition-of-done stages 3 & 5) |
| **Optional cloud** | Deferred. Trigger: multi-machine or S3-scale need (technology-matrix) | A new ADR must define it (OSMO-style backends are the reference pattern, #31) |

Containerization is **deferred**, not banned: a Dockerfile may be added as an *optional* delivery artifact when a
container runtime exists (decision matrix §10), never as a dev prerequisite.

## Processes

| Process | Command (planned) | Notes |
|---|---|---|
| API | `de api --host 127.0.0.1 --port 8000` | Localhost bind is the v1 security boundary (ADR 0009) |
| Workers | `de worker --slots cpu=4,gpu=1` | N processes; GPU slot lease per compute-orchestration.md |
| Dev all-in-one | `de dev` | API + worker pool + auto-reload for local work |
| GC / maintenance | `de gc` (also schedulable as a job) | Orphan blobs, stale tmp, metric retention |
| Doctor | `de doctor` | Env checks: Postgres reachable, disk headroom, GPU visibility, lockfile hash |

## Configuration

Precedence: **environment variables > `.env` file (local, gitignored) > defaults**. Keys (`.env.example` holds
placeholders only):

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

## Data management

- **Backup:** catalog = `pg_dump` (documented runbook); artifacts are immutable → file-level copy is safe; manifests
  make backup verification cheap (hash check).
- **Restore drill:** part of Production Hardening (definition-of-done stage 5).
- **Upgrade:** schema migrations are forward-only, numbered, and run via `de migrate` at deploy; artifact layout
  changes require a new `schema_version` in manifests (storage.md §3).

## Windows notes

- All scripts POSIX-compatible (Git Bash); forward-slash paths; no Linux-only syscalls (`os.replace`, `fcntl`-free).
- Path length / permission caveats documented in the runbook; artifacts use short hash prefixes (storage.md §2).
- Python 3.14 wheel availability verified at tooling time (ADR 0004 note); lockfile pins everything (NFR-011).
