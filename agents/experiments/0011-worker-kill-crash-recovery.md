# EXP-0011: Worker-kill crash recovery — real kills of real workers mid-ingest (NFR-006, F5)

- **Date (UTC):** measured 2026-10-03
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-006 ("No catalog/artifact loss on unclean shutdown; pending jobs recovered on restart", hard), failure-mode F5 (worker crash/kill mid-job), F10's destructive half; [ADR 0005](../decisions/0005-catalog-and-job-queue.md) (at-least-once stance), [ADR 0018](../decisions/0018-episode-quality-signals.md). Stage-5 criterion "Fault injection (worker kill …) with recorded outcomes".
- **Tool:** `scripts/fault_drill.py` — throwaway Postgres per trial, real `data_engine.cli:main` worker process (the code path `de worker` runs, spawned without a launcher wrapper so the killed PID *is* the worker), real 43 MiB hour-long MCAP bag (`var/real-data/so101_pick_place_hour.mcap`, ~13 s ingest), real hard kill (Windows `TerminateProcess` via `Popen.kill()`: no `finally`, no failure path — the same visible state as `kill -9` or an OOM-kill).

## Hypothesis / purpose

The suite already proves the recovery *state machine* (`tests/integration/test_crash_recovery.py`: a row left `running` is requeued by `reap_orphaned_jobs`, a healthy job inside the window is untouched, an orphan without retry budget fails rather than strands). What it cannot prove is the thing NFR-006 actually claims: that a **really killed** worker mid-**real** ingest leaves nothing lost, nothing corrupt, and nothing duplicated. Two kill shapes, because where the kill lands decides what can be stranded:

1. **mid-parse** — killed while the reader parses; nothing published yet. Expectation: clean nothing, plain retry.
2. **on-write** — killed at the first sign of artifact-write activity (the `.pending-*` temp of the streaming copy), the publish-then-register window. Expectation: at worst a stranded temp (never visible as an artifact — publication is an atomic `os.link`), and the retry converges to exactly one episode.

The 900 s presumed-death window (`ORPHANED_JOB_SECONDS`) is not waited out: the victim's `started_at` is back-dated past it before recovery, so the **production reaper SQL runs with its production constant** and the drill measures recovery, not the clock.

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty |
| OS / Python / app | Windows 11 10.0.26200 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\\` |
| fixture | `so101_pick_place_hour.mcap`, 42.9 MiB, SHA-256 `56c53ffe…0051` |
| worker victim | real worker loop (`de worker` code path), hard-killed via `Popen.kill()` |
| recovery | a fresh worker's production reaper sweep (`reap_orphaned_jobs`, 30 s cadence, 900 s window) + ordinary retry |
| verified after recovery | job terminal state, exactly 1 episode / 1 quality row for the source, blob SHA-256 == source file SHA-256, re-ingest of the same source is a no-op (F26) |

## Results

Both trials landed the kill mid-flight (`kill_effective: true` — the job was `running` when the worker died).

### Trial 1 — mid-parse

| Phase | Observation |
|---|---|
| at kill | job `running`; 0 episodes, 0 artifact rows, 0 quality rows, no blobs on disk |
| recovery | reaper reclaimed (attempts → 2), retry **succeeded** in 7.0 s wall |
| after recovery | 1 episode, 1 artifact row, 1 quality row; blob SHA-256 matches the source file |
| duplicate ingest (F26) | fresh job for the same source **succeeded**; episode count still 1 |

Nothing was written before the kill, so recovery is the plain path. **No loss, no duplication.**

### Trial 2 — on-write (the interesting window)

| Phase | Observation |
|---|---|
| at kill | job `running`; 0 episodes, 0 artifact rows; one **stranded `.pending-*` temp** on disk, no published blob |
| recovery | reaper reclaimed (attempts → 2), retry **succeeded** in 6.7 s wall |
| after recovery | 1 episode, 1 artifact row, 1 quality row; published blob SHA-256 matches the source file |
| duplicate ingest (F26) | fresh job for the same source **succeeded**; episode count still 1 |

Exactly the predicted worst case: the kill landed inside the streaming copy, stranding a `.pending-*` temp. The catalog never saw it (publication is atomic `os.link`; the row is written only after), the retry wrote the real blob and registered **exactly one** episode. **No loss, no corruption, no duplication.**

### One defect found by running the drill the way the runbook says to

The drill's first draft started workers via `uv run … de worker`, and the standalone worker **exited on its first loop iteration** with `parent process gone; worker exiting`: `_parent_alive()` read `multiprocessing.parent_process() is None` (not a multiprocessing child) as "orphaned". Every standalone `just worker` / `de worker` has been unable to run its loop since the guard landed — only `de dev`'s children ever worked, which is why the suite (which monkeypatches the guard to `True`) never saw it. **Fixed** (`cli.py`: `parent is None or parent.is_alive()`) with three regression tests covering standalone / live-parent / dead-parent (`tests/unit/test_cli.py`), verified live: a standalone worker now drains the queue. This is the second defect this campaign found that only running the real system surfaces (after EXP-0010e's metrics N+1).

Also found and fixed in the drill tooling itself (recorded so the next reader doesn't trust a bad instrument): `uv run` wraps the worker in a launcher, so `Popen.pid` was the launcher and `terminate()` left the real worker alive claiming jobs — the drill now kills the python process executing the loop. And the on-write kill window is short enough that a kill can land *after* the job finishes; such a trial proves nothing about recovery, so trials re-run until `kill_effective` and the report carries the flag.

## Honest caveat

- Windows `TerminateProcess` is the analog of `kill -9`; a SIGKILL on POSIX is the same class of death. Linux is untested here.
- The drill proves the **worker-crash** half of NFR-006. The other half — "no loss on unclean shutdown" of *Postgres itself* (kill the database mid-request) — is F10's destructive drill and is still stage-5 work.
- The stranded `.pending-*` temp is garbage that nothing sweeps: `de gc` is a stub (documented in [deployment.md](../architecture/deployment.md)). It is invisible to the catalog and harmless to correctness, but a repeated crash can accumulate them; repository-wide reclamation is F15.
- Recovery time (6.7–7.0 s) is dominated by re-ingesting the 43 MiB bag, not by the reaper; the reaper cadence (30 s sweep) dominates in production when a sweep hasn't run yet.

## Trade-offs / verdict

**NFR-006's worker-crash half: verified at the system level.** Two kill shapes, both converging to exactly one episode and one intact artifact with the retry absorbed per the at-least-once stance ([ADR 0005](../decisions/0005-catalog-and-job-queue.md)). The only residue is an invisible temp file. The drill itself paid for itself twice over: it exposed the standalone-worker exit defect (fixed, regression-tested) and it kept the instrument honest (the launcher-kill flaw and the `kill_effective` gate).

**Decision: record + fixes adopted.** `scripts/fault_drill.py` is the reusable drill; `cli.py`'s `_parent_alive` fix and the drill-mechanics fixes are the code changes.

## Follow-ups

- F10's destructive half: stop and restart a real Postgres cluster mid-job (the last unmeasured clause of NFR-006's "unclean shutdown").
- F12 (GPU OOM) is the remaining stage-5 fault named in the criterion; GPU-absent fallback is covered (NFR-007) but an OOM injection is not.
- `de gc` should eventually sweep stranded `.pending-*` temps (F15).
