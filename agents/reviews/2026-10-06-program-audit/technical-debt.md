# Technical debt — ranked, with citations

- **Date:** 2026-10-06 · **Tree:** `693edb3` · Companions: [architecture.md](architecture.md),
  [improvement-ranking.md](improvement-ranking.md).
- Ordered by what would embarrass us first in production. Each item cites the exact files/functions and
  the evidence that established it. Items T-01..T-03 were found by measurement
  ([EXP-0014](../../experiments/0014-foreign-data-ingest-corpus.md)), T-04..T-07 by measuring the surface
  ([EXP-0015](../../experiments/0015-feature-latency-at-three-scales.md),
  [EXP-0016](../../experiments/0016-concurrent-workflow-contention.md),
  [EXP-0017](../../experiments/0017-operator-click-and-attention-budget.md)).

## Correctness debt

### T-01 — The topic name silently decides the quality verdict (major)
`analysis/quality.py` excludes dimensions matching `_GRIPPER = re.compile("grip")`, and
`ingest/readers/mcap_reader.py` prefixes flattened dimension names with the **topic name**
(`<topic>.<path>`). Any busiest multi-dim channel whose topic contains "grip" (e.g.
`/left/gripper/joint_states`) voids the entire verdict: `judged_dims` → 0 → documented degenerate
verdict `unknown`. Proven with byte-identical payloads differing only in topic name
(EXP-0014 D1 isolation fixture). The engine reports a data-quality verdict that is a function of a
naming convention.

### T-02 — One non-finite frame value fails the whole ingest, three times (major)
A single `Infinity` in frame data makes `max` non-finite and the `jsonb` insert in
`catalog/repository.py::register_episode` fail with `InvalidTextRepresentation`. The failure is
classified **retryable** in `jobs/worker.py::_settle_failure`, so the file is re-read 3× and then marked
failed — the operator sees a retried-then-dead job for a deterministic input error. ADR 0023's
non-finite guarantee is honored in `analysis/quality.py::analyze` but not in the reader/registration
path (EXP-0014 D2).

### T-03 — Stored failure messages are `"job handler failed"` (major, operator-facing)
`jobs/worker.py::_settle_failure` persists a literal generic string; the reader's real exception is
logged but never surfaced in the job row, the report, or the UI. `reason_codes.py` already has the
machinery to do better. This is why EXP-0014's D1/D2 took traceback spelunking to diagnose instead of
reading a job report (EXP-0014 D3).

### T-04 — The monitor never runs (major, structural)
`monitoring/service.py::MonitorService.tick` has exactly two callers: the
`POST /api/v1/monitoring/tick` route (`api/app.py::run_monitor_tick`) and tests. Nothing schedules it
(grep-verified across `src/` and `tests/`, EXP-0016), so `/ui/incidents` and `/api/v1/incidents` render
zero rows forever in any real deployment. An entire subsystem — detectors, baselines, incident
lifecycle, notifier — is shipped, tested, and inert.

### T-05 — Cluster archive still occupies live surface
`catalog/clusters.py` (596 lines) + `clustering/{extract,layout,online,proposals}.py` serve a frozen
archive (ADR 0029 supersedes 0026). `api/app.py` carries 5 deprecated 410 handlers plus redirect
shims (`/ui/clusters*`), and the frozen read endpoints are still among the slowest on the API
(`clusters` list 417 ms p95 @10k, EXP-0015). The archive's tables and code need an explicit teardown
slice (planned A6 in [the plan](../../implementation/vocabulary-and-operator-gaps-plan.md)).

## Performance debt

### T-06 — `/ui/vocabulary` does its query work twice (595 ms p95 @10k)
`catalog/vocabulary.py::vocabulary_health` internally re-runs `list_unmapped`, `list_entries`, and the
ranker; the page (`api/app.py` around the `/ui/vocabulary` handler, lines 1428–1447) calls
`list_entries` + `list_unmapped` + `vocabulary_health` separately. 182 ms of the page's 333 ms query
budget is duplicated work, over scans of up to 5,000 rows where the page displays 50 (EXP-0015
decomposition table). This is the product's core page and its slowest recurring surface.

### T-07 — O(table) aggregates per request
- `GET /api/v1/quality/summary` → `catalog/repository.py::_assemble_quality_summary`: 399 ms p95,
  **2.2 MiB** of JSON at 10k episodes (EXP-0015). EXP-0010c already found it the tightest NFR-003
  margin at 181 ms; the HTTP path is worse because it serializes everything.
- `/ui/insights` → `web/pages.py` insights renderer: 389 ms, **5.2 MiB** HTML at 10k (EXP-0015).
- `/ui/schema` → `catalog/introspect.py` per request: 538 ms (EXP-0015).

### T-08 — Vocabulary triage is one page reload per decision
`web/pages.py` vocabulary renderer emits 28 buttons / 27 forms on one page (EXP-0017); every POST is a
full page reload re-running the T-06 query set (including its 182 ms duplicate). 10 strings ≈ 10
clicks ≈ 6 s of waiting. There is no batch accept/map, no keyboard flow, no optimistic advance to the
next queue item.

## Architecture debt

### T-09 — `api/app.py` (1,951 lines) and `web/pages.py` (2,586 lines) are monoliths
Every route, handler, form parser, and page lives in two files. `ui_submit_job` (app.py:618) alone mixes
form parsing, payload construction, validation, rendering, and redirect policy. This is why the UI/API
gap (item 2 in [improvement-ranking.md](improvement-ranking.md)) is hard to close: adding `validate`/
`build`/`export` forms means growing files already past maintainable size.

### T-10 — The UI mutates through a second, hand-parsed front door
`ui_submit_job` parses `application/x-www-form-urlencoded` by hand (`urllib.parse.parse_qs`) to avoid
`python-multipart` (ADR 0014 dependency freeze). Defensible once; multiplying it across every new form
(validate/build/export/batch vocabulary) multiplies hand-rolled parsing and its validation-shape risk.

### T-11 — No hosted CI
[.github](../../../.github) contains only `pull_request_template.md`; the justfile states "this recipe is
the gate". Every guarantee in this repo depends on someone remembering to run `just ci` locally.

### T-12 — Selection semantics hidden in one shared helper
`jobs/worker.py::_selection` — "empty means everything" but *whose* everything differs by job type
(validate: all episodes; build: validated episodes). It is documented and tested, but it is exactly the
kind of implicit default that produces a build over the wrong corpus six months from now.

## Reliability / ops debt

### T-13 — `de gc` is a stub
`cli.py` prints "No garbage-collection work is implemented in the vertical slice." Orphaned blobs
(renamed/merged vocabulary rows don't hold blobs, but failed builds and re-ingests do leave them),
`.pending-*` residue (EXP-0011 found this as the only crash residue), and old metrics JSONL have no
reaper.

### T-14 — Failure/error taxonomy leaks implementation strings
T-03 is the surface symptom; underneath, `_failure_reason_code` + exceptions crossing the
handler boundary lose structure (no `error_type` on the job row, only in logs).

### T-15 — NFR wording known-unsatisfiable, un-revised
EXP-0010d: NFR-005's "queue latency ≤ 1 s" is satisfiable only as dispatch latency; raw queue-position
wait at depth 100 measured 84.95 s (service-rate physics). The NFR needs an owner revision; it currently
documents a promise the design cannot keep.

## Documentation / process debt

### T-16 — Deferred falsifier measurements for the vocabulary bet
`agents/implementation/vocabulary-and-operator-gaps-plan.md` §4 gates: unmapped share > 20 % over a real
corpus and candidate confirm precision < 50 % would falsify the vocabulary-first bet. **Neither has been
measured** (needs a real external corpus). No claim is made anywhere, which is honest — but the bet's
central premise remains unvalidated.

### T-17 — Baseline coverage is thin by policy
HANDOFF §13 requires the owner's green flag before committing benchmark baselines. Two are committed
(`benchmarks/baselines/synthetic-ingest-windows.json`, `mcap-ingest-windows.json`), so `just bench`
regression-checks the ingest workloads only — validation, build/export, vocabulary, and the read/latency
surfaces have no committed baseline and cannot be regression-checked.
