# 0015. Cooperative Job Lifecycle (Retry, Timeout, Cancel)

- **Status:** accepted
- **Date (UTC):** 2026-09-29
- **Deciders:** implementer (agent) / owner

## Context

Requirements F6 (bounded retries with a job deadline) and F7 (operator cancellation) and the state
machine in [../architecture/data-flow.md](../architecture/data-flow.md) §4 were specified from the
start but unimplemented: `submit_job` wrote a row that could only ever be claimed once and failed
permanently, and nothing set `attempts`, `deadline_at`, or `cancel_requested`.

Two structural facts constrained the design. First, the current worker handler
(`SyntheticEpisodeIngestService.ingest`) is a single in-process call with no internal checkpoints, so
a cancel cannot be observed mid-call. Second, the platform is a modular monolith on one machine
(ADR 0010), so there is no second host that could be told to stop work by anything other than a
database flag.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| Count attempts in the worker, retry by re-queueing | Small; no schema or process changes | Retry is only as fair as queue ordering |
| Count attempts in `claim_job` (`WHERE attempts < max_attempts`) | The DB is the single source of truth; a crashed worker cannot resurrect an exhausted job | The counter must be incremented inside the claim |
| Cooperative cancel flag only, checked at attempt boundaries | No signal handling, works cross-process for free | A long single call runs to completion |
| Thread or process signals for immediate cancel | Immediate | Unix-only, breaks the Windows-first host story (ADR 0004/0012), and is unsafe mid-write |

## Decision

- **Attempts are owned by the claim.** `claim_job` selects only rows with
  `attempts < max_attempts` and increments `attempts` in the same `UPDATE ... FOR UPDATE SKIP LOCKED`
  statement. A job whose budget is spent can never be claimed again, even if the worker that was
  supposed to settle it died.
- **The worker settles, it does not retry.** On failure the worker calls `_settle_failure`, which
  picks one of three outcomes in priority order: cancel-requested → `canceled`; attempts remaining →
  `retrying` then back to `queued`; otherwise → `failed`. Keeping the decision in one place means the
  metric, the log, and the state can never disagree.
- **Cancel is cooperative and boundary-only.** `POST /api/v1/jobs/{id}/cancel` cancels a `queued` job
  outright and moves a `running` job to `cancel_requested`. The worker observes the flag when it next
  settles a failure. Mid-call cancellation is explicitly out of scope until a stage has checkpoints;
  recording it as a known limit beats shipping a signal handler that only works on one OS.
- **Deadlines are checked twice.** A claimed job whose `deadline_at` has already passed times out
  before the handler runs, and `reap_expired_deadlines()` sweeps any other non-terminal job whose
  deadline has passed. The first is cheap and local; the second needs no worker to be alive.
- **The retry policy is part of the idempotency request.** `max_attempts` and `deadline_seconds` feed
  the request hash, so replaying a key with a different budget is a conflict rather than a silent
  reuse of the original job.
- **New columns are migrations, not schema edits.** `CREATE TABLE IF NOT EXISTS` never adds a column
  to an existing table, so `attempts`/`max_attempts`/`deadline_at`/`worker_id`/`lease_expires_at` are
  applied by a separate idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` script that runs on
  every startup. `worker_id` and `lease_expires_at` are added now because worker leases are the
  documented next step, and adding a column later would need another migration.

## Addendum (2026-09-29): not every failure is retryable

The decision above says a failed attempt is requeued while budget remains. That is wrong for a
specific and predictable class of failure, and the architecture had already said so: F1 and F3 in
[failure-handling.md](../architecture/failure-handling.md) require that unparseable data and an
invalid payload be *terminal*, because "retrying won't fix data".

The implementation now distinguishes the two. `_TERMINAL_FAILURES` in `jobs/worker.py` lists
`ReaderError`, `InvalidJobPayload`, and `UnsupportedJobType`; `_settle_failure` skips the retry
branch for them and the job goes to `failed` on its first attempt. Everything else keeps its budget.

The classification is attached to the exception types rather than kept in a side table, so a failure
mode nobody has thought about yet is terminal by default. That is the right default: a wasted retry
is cheap, a job that retries forever is not. The synthetic ingest service now validates its own
contract and raises `ReaderError` instead of leaking a bare `KeyError`, so the synthetic path and the
reader path agree on what "this data is not valid" means.

One transition was missing from the state machine as a result. `requeue_for_retry` refuses once the
budget is spent, and `RETRYING` had no edge out, so a job caught in that window was a dead end. The
contract gains `retrying -> failed`. The architecture diagram only ever drew the happy path.

## Addendum (2026-09-30): recovery without a lease

The consequences below flagged two gaps: `reap_expired_deadlines` had no caller, and the
`worker_id` / `lease_expires_at` columns were a stub. Both are now closed, and the second took a
different shape than the columns implied.

The reaper is called from `_worker_loop` on a 30 s interval, alongside `reap_orphaned_jobs`, and
both are opportunistic: a sweep that fails is logged and the loop continues. The loop also catches
around `process_one` itself, because `claim_job` runs before the per-job `try`/`except` and a lost
connection used to propagate out and take the process down.

Recovery uses a **presumed-death timeout**, not the lease columns. A lease only works if the worker
renews it, but the handler is synchronous — the worker thread is inside `process_one` for the whole
job — so a lease could only be written at claim time and would collapse into the same constant with
extra machinery. A real lease needs a heartbeat from a second thread, which is not worth it for a
single local worker. `reap_orphaned_jobs` therefore moves a `running` job whose `started_at` is older
than 15 minutes back to `queued` via `retrying`, or to `failed` when no attempt remains.

The window is deliberately generous because the failure modes are not symmetric. A false reclaim
duplicates work, which the content-addressed artifact and build-hash design mostly absorbs — the
second run converges rather than corrupting. A window that is too tight would reclaim healthy
long-running ingests. The residual risk is a single job that legitimately runs past 15 minutes and is
reclaimed while still alive; that is recorded as a known limit rather than solved.

Superseding note: the stub line about `worker_id` / `lease_expires_at` is now inaccurate. The columns
remain unused, and the presumed-death timeout is the implemented mechanism.

## Consequences

- (+) A job cannot be retried forever, and cannot silently disappear when a worker dies mid-attempt.
- (+) Cancellation is testable end to end and degrades to "runs to completion" instead of losing work.
- (−) A long-running single call is not interruptible. Documented as a known limit; the fix is
  checkpoints inside stage handlers, which is stage-3 work.
- (−) A job requeued by retry is indistinguishable from a freshly submitted one in the queue, so
  `created_at` no longer reflects when work *started*. Job duration is computed from `started_at`.
- (−) `reap_expired_deadlines` is called from the worker loop on a 30 s interval, so a deadline is
  enforced within that window rather than exactly at the deadline.
- (−) A job that legitimately runs longer than the 15-minute presumed-death window can be reclaimed
  while its worker is still alive. The duplicate converges thanks to content addressing, but the two
  runs overlap. A heartbeat-backed lease would remove this at the cost of a second thread.
- Worker leases (`worker_id`, `lease_expires_at`) have columns but no behaviour. The presumed-death
  timeout supersedes the plan recorded here; see the 2026-09-30 addendum.

## Docs updated

- [../architecture/data-flow.md](../architecture/data-flow.md) (§4 now matches the implemented behaviour)
- [../architecture/api.md](../architecture/api.md) (cancel route, `max_attempts`, `deadline_seconds`)
- [../observability/conventions.md](../observability/conventions.md) (three new metrics)
- [../implementation/status.md](../implementation/status.md)
- [../spec/definition-of-done.md](../spec/definition-of-done.md)
