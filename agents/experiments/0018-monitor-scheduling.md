# EXP-0018 — Scheduling the monitor: does the tick fire without a webhook, and what does the cadence cost?

- **Date (UTC):** 2026-10-06
- **Author/agent:** Buffy (implementer), on the P0 backlog of the
  [program audit](../reviews/2026-10-06-program-audit/proposed-v2.md)
- **Status:** done
- **Related:** [ADR 0031](../decisions/0031-monitor-scheduling.md) (the decision this validates),
  [ADR 0020](../decisions/0020-deterministic-monitoring-notifier.md) (the notifier),
  [EXP-0016](0016-concurrent-workflow-contention.md) (the baseline this inherits the cost from)
- **Tree:** commit recorded in the slice that added `MONITOR_INTERVAL_SECONDS`

## Hypothesis / purpose

EXP-0016 measured two things and left both actioned: the monitor tick costs 349–407 ms p50, and
`MonitorService.tick` has exactly two callers — its own HTTP route and the tests — so **no running
deployment ever produces an incident**. This experiment checks the two claims the fix must satisfy,
against the code path the worker loop actually calls:

1. An incident appears without anyone POSTing `POST /api/v1/monitoring/tick`.
2. The scheduled cadence cost is in the range EXP-0016 established, i.e. affordable at 60 s.
3. The catalog lease admits one ticker and makes a second worker yield.

## Change under test

`cli.py::_worker_loop` now builds a `MonitorService` and calls `_monitor_tick` on
`MONITOR_INTERVAL_SECONDS`, which is the notifier's own window (`DEFAULT_WINDOW_SECONDS`, 60 s). The
tick runs inside `PostgresCatalog.monitor_lease()` (a `pg_try_advisory_lock` held for the duration of
the tick). The only variable relative to EXP-0016 is *who calls the tick*: the tick itself, the
feature vector, the rules and the persistence path are unchanged.

## Configuration

- Throwaway database `monitor_probe_<token>` created on the isolated cluster
  (`postgresql://data_engine@127.0.0.1:55432/data_engine`), dropped after the run. Nothing in the dev
  catalog or the repository was written.
- Empty metric sink (a temp `runtime.jsonl`), so the tick builds its feature vector from a cold
  window — the same state a freshly started deployment is in.
- One fault is injected in the *catalog*, not in the code: a job that succeeded producing zero
  episodes, under a completion contract expecting 50 (`Expectation(expected_episodes=50)`). This is a
  real registered detector path (`CONTRACT_BREACH`), not a stub.
- 20 ticks for timing; the incident check is the first scheduled tick.

## Provenance

- Hardware: Windows 11, x86_64, PostgreSQL 17.11 on 127.0.0.1:55432.
- n = 1 incident check, n = 20 timing samples. Single machine, idle apart from the probe.
- Reproduction (this is the exact command; it creates and drops its own database):

```bash
DE_DATABASE_URL=postgresql://data_engine@127.0.0.1:55432/data_engine uv run --all-extras python - <<'PY'
import os, statistics, tempfile, time, uuid
from pathlib import Path
import psycopg
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.cli import _monitor_tick
from data_engine.jobs.state import JobState
from data_engine.monitoring.contracts import Expectation
from data_engine.monitoring.service import MonitorService
import logging

admin = os.environ["DE_DATABASE_URL"]
name = "monitor_probe_" + uuid.uuid4().hex[:8]
with psycopg.connect(admin, autocommit=True) as c:
    c.execute(f'CREATE DATABASE "{name}"')
dsn = admin.rsplit("/", 1)[0] + "/" + name
metrics_path = Path(tempfile.mkdtemp()) / "runtime.jsonl"
settings = Settings(_env_file=None, database_url=dsn, metrics_path=metrics_path)  # type: ignore[arg-type]
initialize_schema(settings)
catalog = PostgresCatalog(settings)

job, _ = catalog.submit_job("ingest", {}, f"probe-{uuid.uuid4()}", "probe")
catalog.claim_job()
catalog.finish_job(str(job["id"]), JobState.SUCCEEDED, result={"episode_id": ""})
catalog.register_contract(str(job["id"]), Expectation(expected_episodes=50).to_dict())

monitor = MonitorService(catalog, metrics_path=metrics_path)
_monitor_tick(monitor, catalog, logging.getLogger("probe"))
print("incidents after one scheduled tick:", catalog.list_incidents(status="open", limit=50))

samples = []
for _ in range(20):
    t0 = time.perf_counter()
    monitor.tick(catalog)
    samples.append((time.perf_counter() - t0) * 1000.0)
print("tick ms  p50=%.1f p95=%.1f mean=%.1f" % (
    statistics.median(samples), sorted(samples)[18], statistics.mean(samples)))

other = PostgresCatalog(settings)
with catalog.monitor_lease() as a, other.monitor_lease() as b:
    print("nested lease acquired:", a, b)
PY
```

The acceptance is also enforced permanently, without a script:
`tests/integration/test_monitoring.py::TestScheduledTick`.

## Results

| Check | Result |
|---|---|
| Incident after one scheduled tick, no HTTP call | **1 open incident** — `CONTRACT_BREACH`, severity `critical`, notify class `notify` |
| Tick cost, n = 20 | **p50 249.5 ms, p95 435.1 ms, mean 283.4 ms** |
| Lease, two catalogs nested | `True`, `False` — the second worker yields |
| Lease released after the block | yes (asserted by `TestMonitorLease`) |

Compared to the EXP-0016 baseline (349–407 ms p50, measured under contention with the HTTP route),
the scheduled tick is in the same band and slightly cheaper on an idle machine with a cold sink. The
60 s cadence therefore spends roughly **0.4 % of one core** on detection, which is the price of the
rule engine plus one sink read, not of the tick's own evaluation (EXP-0003 measured evaluation at
0.2 ms).

## Trade-offs

- **A cold sink and an empty catalog understate the cost.** EXP-0016's 406 ms was measured with a
  live deployment and a populated catalog; the real cadence cost is the larger of the two figures.
  Since both are far below the 60 s budget, the decision does not depend on which number is
  "right" — but the larger one is the one to quote.
- **Only one detector path was exercised.** `CONTRACT_BREACH` is a real path and proves persistence,
  triage gates and the incident row end to end; it says nothing about the precision of the other ten
  rules. That question is backlog B-016 and this experiment does not answer it.
- **The lease is process-agnostic, not tick-aware.** It serializes ticks, so a slow tick delays the
  next one; it does not bound tick duration. A tick that hung would hold the lease until its
  connection closed, which is the same failure mode as a hung reaper and is bounded by the
  catalog's `statement_timeout`.

## Conclusion

**Adopt.** The scheduling primitive works through the exact path the worker loop calls: an incident
exists where none could before, the cost is within the measured baseline and three orders of
magnitude below the cadence, and the lease makes N workers behave like one monitor. `/ui/incidents`
and `/api/v1/incidents` now populate in the documented `just run` deployment.

## Follow-ups

- **B-016 / detection accuracy** becomes measurable for the first time: incidents now exist without a
  manual POST, so the chaos campaign can be scored against a real incident stream.
- **Incident egress (webhooks, plan item B2)** can be sequenced after this, since there are now
  incidents to deliver.
- If a deployment ever runs **no worker** (`de api` alone), there is still no monitor; this is stated
  in the runbook rather than implied.
