# Runbook: Worker Operations

Scope: starting, stopping, inspecting, and recovering the ingest worker on the local and
production-like local tiers ([agents/architecture/deployment.md](../../agents/architecture/deployment.md)).
Windows 11 + Git Bash, no Docker, no service manager required.

## What "the worker" is

There is exactly one worker implementation: a loop that claims one job at a time from the
PostgreSQL queue, runs it, and settles the result. It is not a slot pool — `DE_WORKER_SLOTS`
is read by settings but the deployed worker is single-process, and running N of them is the
supported way to get concurrency (each claims from the same shared queue, which is safe by
design).

| Mode | Command | Use it for |
|---|---|---|
| Worker + API together | `just run` (`de dev`) | normal development; the worker is a **child process** of the API |
| Worker only | `just worker` (`de worker`) | long-running worker next to an already-running API |
| One job then exit | `just worker --once` | draining a queue by hand, or a scheduled batch |
| Stop the API (+ its child worker) | `just stop` | end of session |

## Start

```bash
just doctor          # Postgres reachable and schema initialized, before anything else
just run              # API on 127.0.0.1:8000 + one worker
```

`just run` refuses to start if the port is already held, because a forgotten background
server is the most common way this project "breaks"
([scripts/check_port.py](../../scripts/check_port.py)). If it complains, `just stop` and retry.

To run the worker on its own — the production-like shape, where the API is supervised
separately from the worker:

```bash
just worker           # foreground; Ctrl-C stops it cleanly
```

Backgrounding it under Git Bash (`nohup just worker > var/logs/worker.log 2>&1 &`) is fine
for a laptop that is simply left on. It is **not** supervised: if it dies, nothing restarts
it. For the production-like tier use Windows Task Scheduler with "restart on failure" — see
"Scheduling" below.

## Stop

```bash
just stop
```

This kills whatever is listening on `DE_API_PORT` **only if** the process image looks like
this project's own server (`de`, `python`, `uvicorn`). Anything else holding the port is
reported and left alone. If it reports a foreign process, do not force-kill it: find out
what it is first (`netstat -ano | findstr :8000`, then check the PID).

Because the worker is a child of `de dev`, `just stop` takes the worker with it. A worker
started separately with `just worker` is **not** covered by `just stop` — stop it with
Ctrl-C in its own terminal.

### What stopping costs you

A job that is mid-flight when the worker dies is not lost, but it is also not resumed by
anything. It sits in `running` until another worker's reaper notices it (see below). That
is deliberate: an interrupted ingest is re-run from its immutable source rather than
half-finished, because artifacts are published atomically (F5,
[F5 failure-mode](../../agents/testing/failure-modes.md)).

## Inspect

| Question | Command |
|---|---|
| Is anything waiting? | `curl -s localhost:8000/api/v1/status` — `queue_depth` per state |
| Is a worker alive? | `curl -s localhost:8000/api/v1/metrics` — `worker_heartbeat_age_seconds` (seconds since the last heartbeat; `null` means none was ever recorded) |
| What just happened? | `curl -s 'localhost:8000/api/v1/jobs?limit=50'` , or the `/ui/jobs` page |
| Why did a job fail? | `curl -s localhost:8000/api/v1/failures` , or `/ui/failures` — each row carries a `reason_code` |
| What is the host doing? | `/ui/metrics` — `system_cpu_percent`, `system_memory_used_bytes`, `process_rss_bytes{process_role}`, `system_disk_free_bytes` |
| Raw telemetry | `var/metrics/runtime.jsonl`, queried as documented in [observability conventions](../../agents/observability/conventions.md#queries) |

Heartbeats are written on every claim attempt with `worker_state` `idle` or `busy` and a
value of 0 — the record's *timestamp* is the heartbeat, and the age is derived at read time
so a stale gauge can never be served as current.

## The reapers

The worker loop sweeps the queue every `REAP_INTERVAL_SECONDS` and logs `jobs_reaped` when
it changes anything. It does two things, both of which only matter **after** a worker dies:

- **Expired deadlines** — jobs past `deadline_at` are failed with a timeout reason code, so
  a job whose worker vanished cannot sit in `running` forever (F6).
- **Orphaned jobs** — jobs still claimed by a worker that is no longer claiming anything are
  reclaimed (F5).

Both are on the worker, not the API. **With no worker running, nothing reaps.** If a queue
looks stuck in `running` and jobs are timing out, check for a live worker before anything
else — that ordering is the whole diagnosis.

## The monitor

The worker loop also evaluates one monitoring window every `MONITOR_INTERVAL_SECONDS` (60 s),
under a catalog-held advisory lease so that N workers produce **one tick per interval** rather
than one each (ADR [0031](../../agents/decisions/0031-monitor-scheduling.md)). This is what makes
`/ui/incidents` show anything at all — the rules, control limits and notification classification
were implemented long before anything called them (EXP-0016, EXP-0018).

- **With no worker running, incidents do not appear.** Monitoring is a worker responsibility, like
  reaping. `POST /api/v1/monitoring/tick` still evaluates one window on demand; it is the probe, not
  the schedule, and it deliberately does not take the lease.
- **A failed tick is a lost observation.** It is logged as `monitor_tick_failed` and the loop keeps
  going: monitoring can never fail a job (ADR 0020).
- **What to alert on.** One log line, `incidents_opened`, per tick that wrote rows. The incident
  store itself is the durable record — `curl -s 'localhost:8000/api/v1/incidents?status=open'`.
- **Ticks are not free but are cheap.** A tick measured 249.5 ms p50 / 435.1 ms p95 (EXP-0018); at
  a 60 s cadence that is well under one percent of a core.

## Recovering a stuck queue

1. `just doctor`. If Postgres is down, everything else is noise — restart the cluster with
   `just pg-down && just pg-up`.
2. Check `queue_depth` in `/api/v1/status`. Jobs in `running` with an old `updated_at` and no
   live worker means a reaper is needed: **start a worker** and wait one
   `REAP_INTERVAL_SECONDS`. Do not hand-edit job rows.
3. Jobs in `failed` with `attempts` left are retried automatically on the next claim. Jobs
   that exhausted `max_attempts` are terminal until resubmitted — resubmit the same input
   under a new job id; the catalog is idempotent per episode (F26).
4. If the worker itself is looping on errors, read the log: `event: worker iteration failed`
   with a traceback means the *database call* failed, not the job. The loop backs off
   (   `DB_ERROR_BACKOFF_SECONDS`) and keeps going; a bad job cannot end the worker's life.
   `event: host_sample_failed` is the harmless one: telemetry that cannot be read is
   dropped, not retried.

## Disk pressure

`system_disk_free_bytes` is sampled every 60 s by the worker loop. A full disk surfaces as
a job failing with reason code `IO_DISK_FULL` (logged as `event: job_failed` with that
`reason_code`), and the job registers no artifacts at all — it is not left half-written
(F9). Free space, then resubmit. See [backup-and-restore.md](backup-and-restore.md) for
what to delete safely.

**`just reset` deletes the catalog, its artifacts, and the metrics log.** It refuses to run
without `--yes` and refuses to touch a `*_test` database, but it is not a routine
troubleshooting step. Do not reach for it while investigating a real problem: the data it
removes is the evidence.

## Scheduling

The production-like tier uses Windows Task Scheduler (documented in
[deployment.md](../../agents/architecture/deployment.md)); no service wrapper ships with this
repository, and that is a recorded boundary rather than an omission. Two tasks:

- **API** — runs `bash.exe -lc "just api"`, "run whether user is logged on or not",
  "restart on failure".
- **Worker** — runs `bash.exe -lc "just worker"`, same restart policy.

Because there is no supervisor *inside* the project, the restart policy is what makes a
crashed worker come back. If you would rather not configure the OS scheduler, a foreground
`just worker` in a dedicated terminal is honest and adequate for a single laptop: its
downtime is visible, which a silently-dead cron entry is not.

## Related

- [backup-and-restore.md](backup-and-restore.md) — catalog dump, artifact copy, restore drill
- [../../agents/architecture/deployment.md](../../agents/architecture/deployment.md) — tiers, processes, configuration
- [../../agents/architecture/failure-handling.md](../../agents/architecture/failure-handling.md) — retry, timeout, reaping semantics
- [../../agents/decisions/0015-cooperative-job-lifecycle.md](../../agents/decisions/0015-cooperative-job-lifecycle.md) — the lifecycle state machine
- [../../agents/testing/failure-modes.md](../../agents/testing/failure-modes.md) — F5 (crash), F6 (deadline), F9 (disk full)
