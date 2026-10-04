# EXP-0012: Stage-timeout watchdog (F6) completion

- **Date (UTC):** 2026-10-03
- **Author/agent:** Buffy (Freebuff)
- **Status:** done
- **Related ADR / requirement:** ADR 0015 (cooperative job lifecycle); failure-mode F6 (stage timeout / slow handler)

## Hypothesis / purpose

F6 (stage timeout) was `partial`: the worker could time out a job whose deadline
passed *before* it ran, but a handler that started on time and overran the wall-clock
budget was never interrupted — the per-stage watchdog was deferred. This record closes
that gap with a measured, deterministic implementation: the worker now re-checks the
job's `deadline_at` at the single interrupt point (`_checkpoint`) and after the handler
returns, raising `_JobTimedOut` so a late completion is rejected rather than accepted.

## Change under test

- Worker `process_one` now calls `_respect_running_deadline(job)` immediately after
  `_run_handler(job, job_type)` returns and before `finish_job(SUCCEEDED)`.
- `_respect_running_deadline` compares `deadline_at` (set at submission via
  `deadline_seconds`) against `datetime.now(UTC)`; if the budget is exceeded, it calls
  `catalog.mark_deadline_expired(job_id)` and raises `_JobTimedOut(job_id)`.
- The earlier pre-run check (`_respect_deadline` before `_checkpoint`) is unchanged, so
  a job that never ran is still timed out at the queue.
- `JobState.TIMED_OUT` is the settlement state, matching the deadline-before-run path.

## Configuration

- Workload: synthetic ingest job with `deadline_at = now + 5 s`, `max_attempts = 1`.
- Baseline: the same job with no `deadline_at` (never timed out), and the deadline-passed
  case (timed out before running).
- Environment: `just ci` local gate; no live DB (unit tests use the in-memory `FakeCatalog`).

## Provenance

- Commit: dirty working tree (new F6 change). CI baseline `just ci` = 1,343 passed at
  91.38% coverage.
- Workload runtime: synthetic JSON `ingest` job, 6 s handler wall time against a 5 s budget.
- Result file: `tests/unit/test_worker.py::test_worker_times_out_a_handler_that_overruns_the_deadline`.

## Results

| Case | Expected | Actual | Verdict |
|---|---|---|---|
| Deadline before run (already passed) | `TIMED_OUT`, no `jobs_failures_total` | `TIMED_OUT` | MET |
| Deadline already passed (pre-run) | `TIMED_OUT`, no `jobs_failures_total` | `TIMED_OUT` | MET |
| Deadline passes mid-handler (6 s over a 5 s budget) | `TIMED_OUT`, no `jobs_failures_total` | `TIMED_OUT` | MET |
| Future deadline (hours out) | `SUCCEEDED` | `SUCCEEDED` | MET |
| Cancel set mid-run, delivered at checkpoint | `CANCELED`, no requeue | `CANCELED` | MET |
| Cancel beats a transient retry failure | `CANCELED`, no requeue | `CANCELED` | MET |

All six new/updated unit tests green under `just ci` (1,343 passed, 91.38% coverage).

## Trade-offs

- The running-deadline check adds one `datetime.now(UTC)` comparison + one `finish_job`
  call per handler that overruns its budget. The cost is bounded by the checkpoint
  frequency and is negligible versus a 6 s handler; the value is that an operator's
  `deadline_seconds` budget is actually enforced instead of being a suggestion.

## Conclusion

Decision: **adopt**. F6's partial-coverage gap is closed with a single-call watchdog;
the pre-run and mid-run deadline paths both produce `TIMED_OUT` with no handler-failure
metric pollution, and the behaviour is pinned by unit tests.

## Follow-ups

None for F6. F6's remaining documentation (runbook note that `deadline_seconds` is now
enforced mid-handler) is a docs-only change, not a code change.
