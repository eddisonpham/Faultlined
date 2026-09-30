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
