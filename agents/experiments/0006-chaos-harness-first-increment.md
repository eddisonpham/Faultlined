# EXP-0006: Chaos harness, first increment (B-016)

- **Date:** 2026-09-30
- **Question:** Can the notifier be evaluated against labeled faults without a
  database or a running platform, through the production pipeline unmodified?
- **Method:** `src/data_engine/monitoring/chaos.py`. `ChaosMonitor` overrides
  exactly two seams (`_records`, `_probe`); everything downstream (feature
  builder, baselines, triage, detectors) is the production code. Fault windows
  inject real telemetry records and a real catalog snapshot; a null window is
  the control. `evaluate_window` pins the tick clock to `detectable_at` - the
  earliest instant the aggregation window containing the fault has closed - so
  latency is never credited from a moment detection was impossible. Windows
  score one tick; sequence runs many on one book.
- **Result (unit-level, 14 tests, 0 DB):** `WORKER_LOST` fires at its
  `detectable_at` with latency 0 and persists one incident; it stays silent at
  onset; a late tick scores latency from `detectable_at`. A queue backlog with
  a live worker is not mislabeled `WORKER_LOST` and does not read blind; once
  the queue baselines are warm it surfaces as `METRIC_SHIFT` (the designed
  residual path). Clean windows stay silent. A sustained backlog holds its
  baseline and bumps one incident rather than opening two.
- **Conclusions:** the harness exists and is honest about what it measures.
  **No accuracy figure is claimed yet** - precision/recall per label needs the
  full fault-injection campaign with clean-replicate null bands (plan §6),
  including the real-kill `WORKER_LOST` variant that record replay cannot
  express (plan §6.8).
- **Provenance:** clean tree at commit time; host (Windows, Python 3.14);
  875 unit/contract/integration tests passing; coverage 92.11%.

## Amendment (2026-10-06): the host is replayed too

`ChaosMonitor` now overrides a **third** seam, `_resources` (the file is 17 tests
now; the "14" above was already stale when it was written, at 16 before this
change). The harness injected telemetry and catalog state but left host resources
to the real `sample_resources()`, so every window was partly scored against
whatever machine ran the replay. `memory_used_ratio > 0.92` opens a real
`RESOURCE_DEGRADED` and a development box sits within a percentage point of that
floor. On a loaded machine:

- a null window was not silent - it fired `RESOURCE_DEGRADED`;
- a `WORKER_LOST` window touched two incidents instead of one;
- a warm-backlog window reported `[METRIC_SHIFT, RESOURCE_DEGRADED]`.

So the two properties EXP-0006 claims ("clean windows stay silent", "sustained
faults bump one incident") held or not depending on what else was open, and a
campaign's precision would have described the laptop rather than the detectors.
Replay now pins a healthy host (`REPLAY_HOST`). A resource *fault* is still not
expressible as a `FaultWindow` - recorded here rather than papered over; it needs
a real host fault in the full campaign, like the real-kill `WORKER_LOST` variant
in plan §6.8.

The same leak was found in two tests that drive a real `MonitorService` and
assert an exact signal set (`tests/integration/test_monitoring.py`,
`tests/contract/test_monitoring_api.py`); both now pin the sample through
`tests/conftest.py::pin_a_healthy_host`, which is the shared form of this fix.
