# The 10 highest-leverage improvements — ranked

- **Date:** 2026-10-06 · **Tree:** `693edb3` · Derived only from
  [architecture.md](architecture.md), [current-capabilities.md](current-capabilities.md),
  [technical-debt.md](technical-debt.md), [missing-production-capabilities.md](missing-production-capabilities.md).
- **Selection rule:** leverage = (impact × user value × engineering value) / (difficulty × risk),
  computed against what the code and the measurements actually show — not against what other products
  have. Every item cites the specific files/functions that motivate it. Anything that lost on leverage
  is listed under ["Just below the line"](#just-below-the-line) with its category.

## Ranking

| # | Improvement | Cat. | Impact | User value | Eng. value | Difficulty | Risk | Dependencies |
|---|---|---|---|---|---|---|---|---|
| 1 | Schedule the monitor so incidents exist | F | High | High | High | Low | Low | ADR 0031 |
| 2 | Persist real failure reasons on job rows | D | High | High | High | Low | Low | none |
| 3 | Topic-name must not decide the quality verdict | D | High | High | Med | Low | Low | none |
| 4 | Terminal-fail non-finite frames instead of 3 retries | D | High | Med | High | Low | Low | none |
| 5 | Browser routes for validate / build / export / slice | A | High | High | Med | Med | Low | item 6 (form surface) |
| 6 | `/ui/vocabulary`: stop doing every query twice | E | High | High | Med | Low | Low | none |
| 7 | Batch vocabulary triage (≤ 3 clicks per 10 strings) | H | Med | High | Low | Med | Low | item 6 |
| 8 | Bound `quality/summary` and `/ui/insights` payloads | E | Med | Med | High | Med | Med | measure before choosing (rollup vs pagination) |
| 9 | Hosted CI running `just ci` | C | High | Low | High | Low | Low | a Postgres service |
| 10 | Enforce the local-only trust boundary | G | Med | Low | Med | Low | Low | owner decision on non-local bind policy |

## A. Missing functionality

### 5 — Validate, build, export, and slice creation from the browser
- **Motivating code:** `api/app.py::ui_submit_job` (line 618) constructs only `ingest` and
  `ingest_source` payloads; `POST /api/v1/slices` (line 1064) is JSON-only; the slice and build UI pages
  are read-only. Verified by inventory, [EXP-0017](../../experiments/0017-operator-click-and-attention-budget.md).
- **Why highest-leverage in its class:** the product's stated output is a curated dataset build; today
  the last three steps of the loop exist only behind curl. This is the difference between a tool an
  operator uses and a tool an operator's engineer uses.
- **Acceptance:** ingest → validate → build → export → slice completable with no JSON calls; every new
  form keeps `ui_submit_job`'s properties (same Pydantic models as the API, typed values preserved on
  error, 303 on success).

## B. Architectural weaknesses

No item from this category reaches the top 10 on leverage — the strongest architectural candidate
(splitting `api/app.py` (1,951 lines) / `web/pages.py` (2,586 lines), and escaping hand-parsed forms in
`ui_submit_job`; [technical-debt.md](technical-debt.md) T-09/T-10) is high-effort and low user-visible
impact, so it ranks 11th. It is nonetheless the **prerequisite that keeps items 5 and 7 cheap**: if
form-parsing sprawl shows up while building item 5, promote T-10's `python-multipart` amendment to ADR
0014 immediately rather than after the third hand-rolled parser. Item 1's root cause is also
architectural (the platform has no periodic-execution primitive at all); its fix should create one.

## C. Developer experience

### 9 — Hosted CI that runs `just ci`
- **Motivating code:** [justfile](../../../justfile) `ci:` recipe ("There is no hosted runner on this
  project: this recipe is the gate"); [.github/](../../../.github) contains only `pull_request_template.md`.
- **Why leverage:** every guarantee in this repo — 1,399 tests, strict mypy, hygiene links, OpenAPI drift
  — currently depends on a human remembering to run one command. A workflow with a Postgres service is
  ~30 lines and converts the whole gate from ritual to property.
- **Risk note:** Windows-specific justfile settings (`windows-shell`) mean the runner should invoke the
  underlying `uv` commands directly, not `just ci`, unless the workflow also installs `just`.

## D. Reliability

### 2 — Persist real failure reasons on job rows
- **Motivating code:** `jobs/worker.py::_settle_failure` stores the literal `"job handler failed"`;
  `observability/reason_codes.py` already classifies; `catalog/repository.py::finish_job` persists.
- **Evidence:** [EXP-0014](../../experiments/0014-foreign-data-ingest-corpus.md) D3 — the reader's real
  exception is logged (`error_type` in JSON logs) but never reaches the job row, `job_report`, or
  `/ui/jobs/{id}`, so diagnosing D1/D2 required traceback spelunking instead of reading a report.
- **Difficulty:** low — the exception is in hand at the catch site; sanitize and store `type(exc).__name__`
  + message.

### 3 — Topic name must not decide the quality verdict
- **Motivating code:** `analysis/quality.py::_GRIPPER = re.compile("grip")` (dimension exclusion by name
  substring) + `ingest/readers/mcap_reader.py` (flattened dims named `<topic>.<path>`).
- **Evidence:** EXP-0014 D1 — byte-identical payloads judged `smooth` as `/joint_states` and `unknown`
  as `/left/gripper/joint_states`. `judged_dims` → 0 → degenerate verdict.
- **Impact:** the engine's core claim is trustworthy quality verdicts; this one makes the verdict a
  function of a topic-naming convention.

### 4 — Terminal-fail non-finite frames instead of retrying three times
- **Motivating code:** `catalog/repository.py::register_episode` (`jsonb` insert raises
  `InvalidTextRepresentation` on `Infinity`); `jobs/worker.py::_settle_failure` classifies it retryable;
  ADR 0023's guarantee lives only in `analysis/quality.py::analyze`.
- **Evidence:** EXP-0014 D2 — one `Infinity` → whole ingest fails after 3 full re-reads of the file.
- **Impact:** correctness (guarantee is one layer deep) and cost (deterministic errors burn the retry
  budget that crash recovery needs — EXP-0011 semantics depend on retries meaning "might work").

## E. Performance

### 6 — `/ui/vocabulary`: stop doing every query twice
- **Motivating code:** `catalog/vocabulary.py::vocabulary_health` (line 555) internally re-runs
  `list_unmapped` + `list_entries` + `clustering/ranker.py::candidates`; the page handler
  (`api/app.py` `/ui/vocabulary`, lines 1428–1447) calls `list_entries` + `list_unmapped` +
  `vocabulary_health` separately.
- **Evidence:** [EXP-0015](../../experiments/0015-feature-latency-at-three-scales.md) decomposition:
  182 ms of the page's 333 ms query budget is duplicated work; page p95 595 ms at 10k episodes.
- **Difficulty:** low; **payoff:** this is the page the product's central workflow lives on, hit again
  on every poll (3 s) and every form POST.

### 8 — Bound `quality/summary` and `/ui/insights` payloads
- **Motivating code:** `catalog/repository.py::_assemble_quality_summary` (whole-table aggregate, all
  top-N lists per request); `web/pages.py` insights renderer (unpaginated episode tables).
- **Evidence:** EXP-0015 @10k: `quality/summary` 399 ms / **2.2 MiB** JSON; `/ui/insights` 389 ms /
  **5.2 MiB** HTML. EXP-0010c already flagged `quality_summary` as the tightest NFR-003 margin (181 ms).
- **Why "measure before choosing":** two candidate fixes (pre-aggregated rollups = planned B3 / ADR 0032,
  or response capping + pagination) differ by an order of magnitude in complexity; the choice must be
  justified by a re-measurement per `agents/benchmarking/methodology.md`, not by taste.

## F. Observability

### 1 — Schedule the monitor so incidents exist
- **Motivating code:** `monitoring/service.py::MonitorService.tick` — exactly two callers:
  `api/app.py::run_monitor_tick` (line 1821) and tests (grep-verified, EXP-0016). Compare
  `cli.py::_worker_loop`, which already runs `_reap` and `_sample_host` on intervals and is the natural
  home.
- **Evidence:** EXP-0016 — `/ui/incidents` renders zero rows and will render zero rows forever; the tick
  costs 349–407 ms p50 (~0.2 ms evaluation, rest feature-building + sink read), trivially affordable at
  a 30–60 s cadence.
- **Why #1 overall:** it is the cheapest change with the largest surface — it activates detectors,
  baselines, incident lifecycle and the notifier, i.e. an entire shipped, tested, currently-inert
  subsystem. Requires **ADR 0031** (scheduling semantics; amends ADR 0020's HTTP-triggered shape).
- **Follow-on:** detection accuracy (B-016) then becomes measurable, closing the
  "no precision/recall claim" gap honestly.

## G. Security

### 10 — Enforce the local-only trust boundary
- **Motivating code:** `config.py::api_host` (default `127.0.0.1`, freely settable), no auth anywhere in
  `api/` (grep-verified), and `jobs/worker.py::_run_handler` `ingest_source` reading **any filesystem
  path the worker can access** (`payload.source`), plus open mutations (`DELETE /api/v1/slices/{id}`,
  vocabulary merge/split).
- **Why not "add auth" (that would be cargo-culting):** for a single-operator local-first tool, an auth
  system is speculative weight. The leverage is in making the existing trust boundary *enforced* rather
  than *implicit*: refuse to bind non-loopback unless an explicit opt-out env is set, and document the
  `ingest_source` path-read semantics in the runbook. ~20 lines + a doc, and it removes the footgun that
  turns a local tool into a remote file-read service.
- **Trigger to promote:** the moment a second human or a shared host enters the picture
  ([missing-production-capabilities.md](missing-production-capabilities.md) §1) this becomes real
  authn/authz work with actor attribution on vocabulary events.

## H. UI/UX

### 7 — Batch vocabulary triage: ≤ 3 clicks per 10 strings
- **Motivating code:** `web/pages.py` vocabulary renderer (28 buttons / 27 forms, one POST + full page
  reload per decision, each re-running item 6's query set); queue advance is not modeled at all.
- **Evidence:** [EXP-0017](../../experiments/0017-operator-click-and-attention-budget.md) — 10 strings ≈
  10 clicks ≈ 6 s of pure page-load waiting, on top of the attention cost of 10 full re-renders. The
  product's core loop is *decision throughput*; its core page is optimized for one decision per reload.
- **Shape:** multi-select + one POST (reuse `POST /ui/vocabulary/accept|map|dismiss` semantics in one
  batch endpoint), land on the refreshed queue with the next candidate focused; keyboard `a`/`m`/`d`
  is a cheap follow-on. Leverages item 5's form machinery.

## Just below the line

| Rank | Item | Cat. | Why it lost |
|---|---|---|---|
| 11 | Split `api/app.py` / `web/pages.py`, replace hand-parsed forms (T-09/T-10) | B | high effort, no user-visible win alone — but promote if form sprawl appears during item 5 |
| 12 | Format adapters: ROS 2 bag + CDR/protobuf MCAP (EXP-0014 showed both mishandled) | A | highest *strategic* value and the one place real spend is justified (2026-10-04 verdict P3), but multi-week and independent of items 1–10 |
| 13 | `de gc` garbage collection (T-13) | D | cost accrues slowly; becomes urgent on shared machines |
| 14 | Batch/directory ingest (one job per path today) | A | item 5 first; the API already exists as the escape hatch |
| 15 | Incident egress (webhooks, B2, needs ADR 0031) | A | worthless until item 1 makes incidents exist — sequence after |
| 16 | Cluster-archive teardown (A6, T-05) | B | hygiene; keeps the slowest frozen reads (417 ms) alive but harms no user |
| 17 | Build diff (B5) + decision-log export | A | real but downstream of the core loop working end to end |

## Category index

- **A Missing functionality:** 5 (just below: 12, 14, 15, 17)
- **B Architectural weaknesses:** none in the top 10 — see the note above and ranks 11, 16
- **C Developer experience:** 9
- **D Reliability:** 2, 3, 4 (just below: 13)
- **E Performance:** 6, 8
- **F Observability:** 1
- **G Security:** 10
- **H UI/UX:** 7

## What was deliberately not proposed

No auth system, no SPA rewrite, no Kubernetes/container story, no microservice split, no message broker,
no model inference in the decision path — each would be a feature copied from other applications rather
than a response to a defect or measurement in this codebase (CLAUDE.md scope guard, ADR 0014, ADR 0020).
