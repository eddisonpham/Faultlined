# ADR 0031: The monitor runs on a schedule the worker pool holds, one tick at a time

- **Date:** 2026-10-06
- **Status:** accepted
- **Stage:** post-stage-5 production-hardening work
  ([program audit](../reviews/2026-10-06-program-audit/proposed-v2.md) P0 item 4,
  [improvement ranking](../reviews/2026-10-06-program-audit/improvement-ranking.md) #1)
- **Related:** [ADR 0020](0020-deterministic-monitoring-notifier.md) (the notifier is deterministic),
  [ADR 0015](0015-cooperative-job-lifecycle.md) (the worker loop already runs the reapers),
  [ADR 0010](0010-modular-monolith-and-workers.md) (out-of-process workers),
  [ADR 0008](0008-observability.md) (telemetry cannot stop the worker)

## Context

The monitoring subsystem — eleven rules, per-scope control limits, the incident lifecycle, the
notifier, completion contracts — is implemented, tested, and **inert**. `MonitorService.tick` had
exactly two callers: its own route (`POST /api/v1/monitoring/tick`) and the tests. Nothing in any
running deployment ever called it, so `/ui/incidents` rendered zero rows and would render zero rows
forever, and the detection-accuracy question could not even be posed (EXP-0016).

The subsystem was built around one assumption that the code comments state outright
(`monitoring/service.py`: "The loop is a scheduler's job, not a webhook's") and that no decision
record ever specified: that *something* would schedule `tick`. This ADR is that something.

Constraints that shaped the choice:

1. The monitor is out of band by design (ADR 0020): a tick that raises is a lost observation, never
   a lost job. Scheduling may not couple monitoring health to job health.
2. A tick reads the metric sink, probes the catalog, and persists incidents and baselines. It is
   **not idempotent**: it bumps an incident's `occurrence_count` and observes the baseline book, so
   N schedulers produce N times the observations unless serialized.
3. Running N workers is a supported configuration
   ([deployment](../architecture/deployment.md): "Run N of them for concurrency").
4. A tick cost 349–407 ms p50 in EXP-0016 (roughly 0.2 ms of evaluation, the rest window building and
   one sink read), i.e. trivially affordable at a 60 s cadence.
5. There is no scheduler dependency and no desire for one (ADR 0012): the process model is host
   processes started by `just`.

## Decision

1. **The worker loop schedules the tick.** `cli.py::_worker_loop` already runs `_reap` (deadlines and
   dead workers) and `_sample_host` (host gauges) on their own intervals; the monitor joins them on
   `MONITOR_INTERVAL_SECONDS`, which is the notifier's own window (`DEFAULT_WINDOW_SECONDS`, 60 s).
   A shorter cadence re-evaluates records the previous tick already saw; a longer one lets a
   sustained fault sit unobserved between ticks.

2. **One pool, one tick.** The tick runs inside `PostgresCatalog.monitor_lease()`, a session-scoped
   advisory lock. A worker that cannot take the lease skips its turn; a worker killed mid-tick
   releases the lock with its connection. This is what keeps `occurrence_count` proportional to the
   number of faults rather than to the number of workers, and it now conforms to the deployment
   doc's supported N-worker shape instead of quietly violating it.

3. **The webhook is a probe, not a scheduler.** `POST /api/v1/monitoring/tick` stays, unchanged, as
   the deterministic path for a test, a benchmark, or an operator asking "what would it say right
   now". It does not take the lease: a manual probe must never be silently suppressed.

4. **Failure is a lost observation.** `_monitor_tick` catches everything and logs
   (`monitor_tick_failed` / the monitor's own `monitor_*` warnings), exactly as `_sample_host` and
   `_reap` do. Incident creation is logged once per tick that writes rows
   (`incidents_opened`), because an incident nobody notices is the failure mode this whole
   subsystem exists to prevent.

5. **The cadence and its cost are recorded as an experiment.** The baseline is EXP-0016's
   349–407 ms p50; the scheduling slice records what the scheduled cadence actually costs on a
   running deployment, so the number in the docs is measured rather than inherited.

## Alternatives considered

- **A dedicated `de monitor` process.** Rejected as the *primary* mechanism: it adds a third process
  to start and to forget, and the runbook's supported paths (`just run` = API + worker, `just worker`)
  would not start it. It remains possible later without changing this decision, because the
  scheduling primitive is a loop plus a lease rather than anything worker-specific.
- **A FastAPI lifespan background task.** Rejected: it ties monitoring to the API process, so
  `just worker` with no API gets no monitoring, and a job-processing worker and the API would both
  need the lease anyway — the same coordination problem, in a worse place (ADR 0010 keeps the
  worker the process that does work).
- **No lease, dedup by fingerprint.** The incident *rows* would still dedup (the partial unique
  index guarantees it), so this is not a correctness failure. It was rejected because
  `occurrence_count` and the baseline book would still scale with the worker count, which makes a
  user-visible number a function of deployment size.
- **An external scheduler (cron, Task Scheduler, a queue).** Rejected: new operational surface for
  a 350 ms computation, and it reintroduces "someone must configure it", which is the exact failure
  this ADR closes.

## Consequences

- `/ui/incidents` and `/api/v1/incidents` begin populating in any deployment that runs a worker —
  including `just run`, which is the documented quickstart.
- `api/app.py`'s `app.state.monitor` and the worker's monitor are separate `MonitorService`
  instances. Their in-memory baseline books are per-process; the catalog's `load_baselines` /
  `save_baselines` remain the shared truth, which the lease now keeps from being written
  concurrently.
- `de api` alone does not monitor. This is stated in the runbook rather than implied.
- Detection accuracy (backlog B-016) becomes measurable for the first time, because incidents
  exist without a manual POST. No precision/recall figure is claimed by this ADR.
- Incident egress (webhooks, plan item B2) can now be sequenced after this, since there are
  incidents to deliver.

## Docs updated

- [deployment](../architecture/deployment.md) (what schedules the monitor)
- [HANDOFF](../HANDOFF.md), [implementation status](../implementation/status.md)
- `docs/runbooks/worker-operations.md` (monitoring is a worker responsibility)

## Amendment (2026-10-06): health answers for the process that ticked

The Consequences section above notes that the API and the worker hold **separate**
`MonitorService` instances. Running the real deployment (`just run`) showed why that is
not merely an implementation detail: `/api/v1/monitoring/health` returned
`last_tick_at: null` while the worker was visibly ticking (`monitor_tick_seconds` written
twice in the shared sink), and `/ui/incidents` rendered `blind: no` because `health()`
never carried a `blind` key at all. An operator checking the one endpoint whose stated
purpose is to catch a stopped monitor would have concluded the monitor was stopped, and
the page would have claimed the monitor could see when it could not.

Both facts are now read back from the metric sink — the one record every process shares
(ADR 0017) — when the serving process has not ticked itself:

- `last_tick_at` is the newest `monitor_tick_seconds` record, or `null` when no tick has
  run anywhere.
- `blind` is the newest `monitor_blind` value: `true`, `false`, or `null` when no tick
  has run. `null` is deliberately distinct from `false`; the incidents page renders it
  `unknown` rather than as a healthy "no".

The read is a bounded tail (`HEALTH_TAIL_RECORDS = 20_000`), because health is served on a
page load and a full-history scan would make the health check the slowest thing on the
page. In-process values still win when this process has ticked - and a process that has
ticked reads nothing at all. One tail read serves both facts: they were read separately,
which parsed the same window twice on every call, measured at 42 ms p50 on a 5k-record sink
and 97 ms at the bound, half of it duplicated work on a page that polls. Tests:
`tests/unit/test_monitoring_health.py` (cross-process read-back, most-recent-wins, the
never-ticked case, and the tail bound); the monitoring contract test now points the app
at an isolated sink so it cannot assert against a developer's own telemetry file.
