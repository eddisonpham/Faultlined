# Handoff

**Status: Scaffolding; review verdict is accept-with-follow-ups (2026-09-28).** The project is **Faultlined**. All engineering findings are resolved and gated; the synthetic-ingest benchmark baseline is measured and committed (2026-09-28, owner-authorized — [§13](#13-baseline-runbook-owner-green-flag-required)). See [engineering review](reviews/2026-09-28-scaffolding.md) and [definition of done](spec/definition-of-done.md). All work is committed on `main` and pushed. Do not tag `scaffold-complete` until the residual verifications below are closed.

**Update 2026-09-29 (stage 2 / MVP).** Four slices landed on `main` since the scaffolding tag: the minimal server-rendered UI ([ADR 0014](decisions/0014-minimal-ui-server-rendered.md)), the committed OpenAPI contract with a drift gate, the job lifecycle — bounded retries, job deadlines, cooperative cancellation, and terminal-vs-retryable failure classification ([ADR 0015](decisions/0015-cooperative-job-lifecycle.md)) — and **real-format ingest**: a LeRobot reader covering v2.1 and v3.0, exercised end to end against real Hub datasets.

**Update 2026-09-29 (validation + run intelligence).** Two further slices landed: **validation** — hashed JSON validation profiles, a 7-rule engine, quarantine and re-validate without re-ingest ([ADR 0016](decisions/0016-json-validation-profiles.md)) — and **run intelligence** ([slice plan](implementation/run-intelligence-slice.md), [EXP-0002](experiments/0002-run-intelligence-workloads.md)): runtime metrics aggregation and `GET /api/v1/metrics` ([ADR 0017](decisions/0017-runtime-metrics-aggregation.md)), episode motion-quality analytics computed at ingest ([ADR 0018](decisions/0018-episode-quality-signals.md)), an instrument UI (`/ui/metrics`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`), and run-inspection API surfaces — episode catalog with quality filters, per-episode validation verdicts and violations, job → episodes reverse lineage — each latency-measured in isolated benchmark processes ([ADR 0019](decisions/0019-benchmark-workload-isolation.md)). The suite is at 308 passing with 92.23% coverage, 22 of them against a live Postgres. The next MVP criteria are the rest of the `ingest → validation → indexing → builds` workflow: indexing and builds are still open, and MCAP ingest is the remaining format.

**Update 2026-09-29 (curated layer + deterministic notifier).** Two more slices. The **curated build-ready layer** adds named curation slices (`/api/v1/slices`, `/ui/slices`) whose membership is recomputed on read so a saved view never goes stale, plus a read-only failures view (`/api/v1/failures`, `/ui/failures`). The **monitoring notifier** ([ADR 0020](decisions/0020-deterministic-monitoring-notifier.md), [plan](implementation/automated-monitoring-plan.md)) watches all of it for unattended curation. It is **deterministic by decision**: no trained model, no LLM call, no network in the detection path. Eleven rules and per-scope EWMA/median-MAD control limits over a versioned 26-feature vector, completion contracts as the anchor check, triage with fingerprinting, dedup, cooldown, evidence and severity gates, and a hard alert budget. `POST /api/v1/monitoring/tick` runs one window on demand; `/ui/incidents` and `GET /api/v1/monitoring/health` make the queue and the monitor's own blindness observable. Measured in isolation ([EXP-0003](experiments/0003-deterministic-notifier-latency.md)): `monitor-evaluate` P50 **0.2 ms**/tick, `api-incidents-catalog` P50 7.4 ms. Suite is at 557 passing, 93.31% coverage.

**Update 2026-09-29 (instrument UI pass).** The visual-design work ADR 0014 deferred has landed ([ADR 0021](decisions/0021-frontend-instrument-pass.md)), and it fixed three things that were broken rather than merely plain. **The vendored themes did nothing** — the CRT scanlines, phosphor glow and panel treatment were live CSS targeting class hooks the markup never used, so all four themes rendered as plain text; the hooks are now in the markup, verified by computed-style audit in a real browser. **A dead backend looked exactly like a healthy one** — the poller swallowed fetch errors, so a laptop that had lost the server showed stale numbers as if current; `web/app.js` now owns a visible failure policy (hazard-striped banner, LED state, stale clock, exponential backoff, permanent-4xx give-up, and the last good render is never discarded), with every failure *and* the recovery written to an `aria-live` region. **`/ui` errors returned raw JSON** — they now render a full error page with the correlation id in the operator's theme, while `/api/v1` keeps its JSON contract unchanged. `/docs` and `/redoc` are gone; `/openapi.json` stays for the drift gate. Also: skip link, table captions, chart and meter labels, click-to-copy identifiers, keyboard navigation, and no animation that ignores `prefers-reduced-motion`. Nine inlined script blobs collapsed into one file. Suite is at 601 passing.

**Follow-up pass, same day.** Reviewing that against real browser screenshots found three defects that reading the source had not. **Sticky column headers sat under the nav** — `top: 2.4rem` was a hardcoded guess and the nav measures 45px, so every header pinned 6.6px above the nav's bottom edge and overlapped the rows it labelled; the offset is now measured at runtime. **Charts filled 61% of their panel** from a `max-width: 46rem` cap; removed, now 97%. **The status colour was a sore thumb** — the vendored `--fine-use-success` is `#00ff00` and `core.css` applies it at full weight, so one neon `ok` sat in a row of grey numbers and read as a highlighted button; inline status words now use a per-theme ramp derived from Okabe-Ito, desaturated and pulled toward each theme's background, with no ramp using the theme accent. The `live` LED is hidden until it has news. Motion is either a published **Material 3 v0.192 token** or, for the loading indicator, an actual damped harmonic oscillator integrated per frame (k=170 c=14 m=1, zeta=0.537, ~13.5% overshoot — the first constants gave zeta=0.85 and the physics was invisible). `@playwright/mcp` is registered in `.mcp.json`; it is what found all three, because a `max-width` that renders fine and a `top` 6px too high both look correct in the source.

**Update 2026-09-29 (MVP complete).** The last two open MVP criteria closed, and both closed with
evidence rather than with a caveat. **Builds are real**: `builds/service.py` makes a build's identity
the SHA-256 of its own manifest and nothing else — no timestamp is hashed, episodes are sorted by id,
the validation policy is cited by content address, the code commit is recorded — so rebuilding the
same selection under the same policy and commit produces the same `bld_` address *by construction*.
NFR-004 is asserted by 13 tests, each pinning a way the hash can change without the data changing.
`builds`/`build_episodes` carry membership, `lineage_edges` the graph, and
`GET /api/v1/episodes/{id}/builds` answers FR-008's reverse direction. **The MCAP reader exists**
([ADR 0022](decisions/0022-mcap-ingest-reader.md)) and is a second entry in the reader registry with
**no core change** — which is the first real test of the promise `registry.py` had been making. And
**the first real-format benchmark baseline is committed** ([EXP-0004](experiments/0004-mcap-ingest-baseline.md),
`benchmarks/baselines/mcap-ingest-windows.json`).

Three findings from this pass are worth more than the features, because each is a measurement that
corrected something we believed:

- **A latent worker bug had been failing every non-ingest job.** `process_one` read
  `result["episode_id"]` unconditionally, so every `validate` and every `build` raised `KeyError`
  *after* doing its work correctly and was then marked failed. Nothing had caught it because nothing
  had run a non-ingest job through the live worker. Regression test in `tests/unit/test_job_result_shape.py`.
- **Ingest is 8× short of its target and the cost is ours.** MCAP ingest measures 6.21 MiB/s against a
  provisional 50 MB/s. The attribution: the MCAP container alone reads at 92 MiB/s, JSON decode at
  58 MiB/s, and `_dimensions` — which rebuilds a dotted path string per numeric leaf, 540 000 times
  for one 10-minute log — is **51% of total ingest time**, roughly 8× the JSON parse it operates on.
  Content-addressing and writing 20 MiB costs 65 ms, so "the disk is slow" would have been wrong by
  an order of magnitude. The fix is backlog B-019 and the next stage starts there.
- **Two selection rules in the MCAP reader were wrong on the first run and look obviously right.**
  Frame rate was `messages / span`, which reads a 50 Hz stream as 50.05 Hz and would let a
  `max_fps: 50` rule quarantine correct data. Motion quality was read from the busiest topic, which on
  a real log is a 1-dimension gripper command that `analyze` excludes from the verdict *by design* —
  so it reported `unknown` on a log with an 18-dimension joint stream in it. Both rules are now
  stated in terms of what the analyser can use, and both are asserted by tests.

**What the notifier deliberately does not do yet.** No detection-accuracy figure exists: that needs the chaos harness (plan §6 — scripted operator action space plus fault injection against the real system, with clean-replicate null bands) and it is backlog B-016, so no precision or recall is claimed. Outbound email is unimplemented and disabled pending explicit owner authorization. Coverage of the real fault space is the one number that cannot be obtained, and every report says so in its header.

**Update 2026-09-29 (end-to-end run).** MVP criteria being checked is not the same as the pipeline
working, so the database was truncated to 12 empty tables, the artifacts and metrics log deleted, and
the whole thing driven again from nothing across three input formats ([review](reviews/2026-09-29-mvp-end-to-end-run.md)).
Three things came out of it.

**Five defects had shipped, all in the wiring around correct code.** A build included the
quarantined episode the validator had just rejected — `BuildPayload`'s own docstring promised the
opposite and no code implemented it. The build manifest cited its policy by an address that was
always `""`, because `ValidationProfile.content_hash` was a field nothing ever set. Six read paths
reported an episode's length from the quality sample, so a 72 600-message MCAP log displayed as an
816-frame episode — correct for every non-streaming reader, which is why it held. Reverse lineage
selected four columns against a seven-field schema and returned three permanently null fields. A
terminal failure spent three attempts and inflated the failure metric enough to trip the notifier.
All five are fixed, each pinned by a test in `tests/unit/test_end_to_end_defects.py`, and the whole
sequence was re-run to 42/42 assertions.

**The lesson is the one worth keeping.** `DatasetBuilder` is pure and has 13 determinism tests, and
it was never wrong. Every defect was in the selection, the serialization, or the query *around* it.
Line coverage was 92% throughout. "Pure core, unit-tested" is not coverage of a feature, and the
next thing this project should build is an end-to-end test that exercises the wiring over HTTP
rather than more tests of the core.

**What held.** Rebuild determinism through the live queue and a real database — identical `bld_`
address, no second row; a different policy, a different address. The MCAP reader needed no change to
the ingest service, validation, the catalog, the API, or the queue: one list entry, which is the
architecture finally paying off rather than promising to. And the notifier caught a real fault during
the run, before anyone read the logs.

## 1. Current problem definition

Build a local-first robot episode data engine to ingest, validate, index/version, curate and build reproducible robot datasets (MCAP / LeRobot) with lineage for ML workloads. The platform is the product; models are workloads. The current vertical slice accepts only a small synthetic JSON episode, not real robotics data. See [problem](spec/problem.md) and [requirements](spec/requirements.md).

## 2. Current architecture

Python modular monolith, FastAPI API, PostgreSQL catalog/job table, one separate local worker process, content-addressed local filesystem artifact store, stdlib JSON logging, psutil host snapshots with optional NVML sampling. The phase 05 path is API submit → Postgres queue → worker → synthetic canonical JSON artifact → episode/lineage catalog → API read. Current schema initialization is inline idempotent DDL, not migrations; artifact and DB updates are not a cross-store atomic transaction. Benchmark foundations are separate and use an in-memory catalog. See [architecture overview](architecture/overview.md), [data flow](architecture/data-flow.md), and [vertical slice](implementation/vertical-slice.md).

## 3. Repository structure

- `src/data_engine/`: API, catalog, jobs/worker, synthetic ingest, artifact storage, config, observability, curation vocabulary, monitoring notifier, web (server-rendered pages + one client runtime), CLI.
- `benchmarks/`: result schema, provenance capture, statistics, synthetic harness; no accepted baseline.
- `tests/`: unit, contract, integration, and E2E tests.
- `agents/`: persistent specifications, architecture, implementation status, test plans, benchmarking, observability, ADRs, reviews, prompts, experiments.
- `.github/workflows/`: CI and hygiene workflows currently duplicate the same quality job.

See [repo layout](architecture/repo-layout.md).

## 4. Technology stack

Python 3.14, uv/uv.lock, just, Ruff, mypy, pytest/pytest-cov, FastAPI/Pydantic, psycopg 3/PostgreSQL 17+, PyArrow and MCAP dependencies (not yet used for real readers), psutil, optional `nvidia-ml-py` for NVML. Host development is the supported local path; Docker is optional. Decisions: [ADRs 0001–0021](decisions/README.md).

## 5. Implementation status — works / stubbed

**Implemented foundations (current checkout):**
- FastAPI POST job submission and GET job/episode/health subset; correlation middleware and typed request/response schema.
- Postgres schema/repository for jobs, artifacts, episodes, and lineage; idempotent submission and atomic queue claim; queue-depth query; **job lifecycle** — attempt counting in the claim, `max_attempts` / `deadline_seconds` on submission, cooperative cancel, deadline sweep, and terminal-vs-retryable failure classification (ADR 0015).
- **Real-format ingest, both formats**: `ingest_source` job type + a sniff-based registry holding two readers. LeRobot (v2.1 and v3.0) is verified end to end against `lerobot/svla_so101_pickplace` and `yaak-ai/lerobot-driving-school`. MCAP ([ADR 0022](decisions/0022-mcap-ingest-reader.md)) reads a bag as one episode, with the frame rate taken from the busiest topic's interval count and motion quality from the busiest topic with more than one dimension; statistics are exact and streaming, and the quality sample is a self-halving window so peak memory is a function of topic count rather than log length. Episode identity is `(source_hash, episode_key)`, so many episodes in one Parquet shard each get a catalog row while the file itself is stored once.
- **Content-addressed builds** (`builds/`): a `build` job over a selection, manifest hashed alone, membership in `build_episodes`, graph in `lineage_edges`, read back at `GET /api/v1/builds`, `GET /api/v1/builds/{hash}` and `GET /api/v1/episodes/{id}/builds`. Rebuilding the same selection reproduces the same address (NFR-004, asserted).
- Server-rendered UI at `/ui` (Status / Jobs / Artifacts) with a committed OpenAPI contract and a drift check in `just ci` (ADR 0014), extended by the instrument pass in ADR 0021: a bezel/silkscreen/LED skin, the vendored CRT theme hooks actually adopted, one `web/app.js` runtime with a visible failure policy, and HTML error pages for `/ui` routes. `/docs` and `/redoc` are removed.
- One ingest worker process; atomic SHA-256 filesystem artifact writes; synthetic JSON canonicalization and lineage readback.
- Unit/contract/E2E tests and Postgres-marked integration tests.
- JSON log formatter, request/job correlation context, psutil/NVML host telemetry with nullable fallbacks.
- **Runtime metrics emitted at call sites**: queue depth, queue/run time, stage duration, failures by reason code, retries, cancellations, timeouts, episode and artifact counters, API request latency, catalog query timing, worker heartbeats (`RuntimeMetrics`, JSONL sink configured by `DE_METRICS_PATH`). Read-back aggregation into percentile summaries, time-bucketed series, and heartbeat ages serves `GET /api/v1/metrics` and the UI (ADR 0017).
- **Validation and quality**: hashed JSON validation profiles with a 7-rule engine, quarantine + re-validate (ADR 0016); motion-quality signals (movement, jerk, stall ratio, length z-score, absolute σ-band verdict) computed at ingest and persisted per episode (ADR 0018).
- **Run inspection**: `GET /api/v1/episodes` (state/quality-flag filters), `GET /api/v1/episodes/{id}/quality` and `/validation`, `GET /api/v1/quality/summary`, `GET /api/v1/jobs/{id}/episodes` (reverse lineage), `GET /api/v1/jobs/{id}/report` (run triage card, also on the job page), `GET /api/v1/episodes/export` (curated manifest for dataset builds); UI pages `/ui/metrics`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`.
- **Curated build-ready layer**: named slices (`GET/POST /api/v1/slices`, `GET/PATCH/DELETE /api/v1/slices/{id}`, `GET /api/v1/slices/{id}/manifest`) store a declarative `{state, flag}` filter and **recompute membership on read**, so a saved curation view never goes stale; the manifest carries content identity (source/artifact hashes) for a downstream dataset build. A read-only failures view (`GET /api/v1/failures`, `/failures/episodes`) aggregates reason-code, profile, and format failure counts and lists quarantined episodes. UI: `/ui/slices`, `/ui/failures`. Filter vocabulary is shared in `data_engine/curation.py`. Plan: [telldown plan](implementation/telldown-plan.md).
- Benchmark schema v2, deterministic statistics, provenance capture, and a CLI that validates results and baselines, refuses to publish from a failed run, and fails closed with an actionable message.

**Not implemented or incomplete:**
- **Materialising a build as LeRobot v3 dataset files on disk** — the manifest and its lineage are real, the dataset directory is not. **CDR/protobuf MCAP payload decoding** (JSON channels only, by decision — a guessed struct layout would put wrong numbers in the catalog) and **multi-session bag segmentation** (a file is one episode, deliberately). Parquet metadata indexing, workload execution, retry backoff, worker leases and lease-based heartbeats (heartbeat *records* exist), mid-run cancellation, GC, formal DB migrations (schema changes are still inline idempotent DDL plus an `ALTER TABLE` block), and committed baselines for the run-intelligence workloads (measured in EXP-0002; committing them needs the owner's green flag).

See [implementation status](implementation/status.md), [failure modes](testing/failure-modes.md), and [review findings](reviews/2026-09-28-scaffolding.md).

## 6. Known technical risks

1. **Benchmark baseline is measured and committed** (2026-09-28, authorized): P50 0.6084 ms, 10 trials, 0 failures at `f7ffbeb`. See [§13](#13-baseline-runbook-owner-green-flag-required) and [EXP-0001](experiments/0001-synthetic-ingest-baseline.md). It is a regression tripwire, not a performance target: observed run-to-run spread was ~24%.
2. **Hardware matching is exact.** A baseline only compares on a byte-identical CPU/RAM/GPU/OS/disk profile; on a different machine the harness refuses to compare. Revisit with evidence before gating CI.
3. **Postgres path is now verified.** `just pg-up` starts an isolated cluster in gitignored `var/pgdata` on port 55432 using local trust auth, so the full suite runs with nothing skipped and the DSN holds no credential. The owner's system PostgreSQL on 5432 is untouched and its superuser password remains unknown; that instance is still unverified.
4. **Crash consistency:** artifact publication and DB catalog writes are separate transactions; worker crash recovery, retries, leases, and orphan GC remain future work.
5. **Clean-clone validation and the history-wide secrets scan are done** (2026-09-28). A clean clone runs `just setup`, `just ci` (95 passed) and `just bench` green; 15 commits contain no credential, `.env` was never tracked, and the only DSN in history is the `USER:PASSWORD` placeholder. No `.env` was opened. This surfaced the `--all-extras` defect: `uv run` pruned the GPU extra, so a fresh clone reported no GPU and `just bench` refused to compare.

## 7. Highest-priority next steps

1. ~~Baseline runbook~~ — done 2026-09-28 with owner authorization ([§13](#13-baseline-runbook-owner-green-flag-required)). New workload baselines (EXP-0002 backlog rows B-003/B-012–B-015) need the same green flag before any is committed.
2. Optionally verify the slice against the owner's own system PostgreSQL (port 5432) by putting its DSN in `.env`; the isolated `just pg-up` cluster already covers this.
3. ~~Clean-clone validation and secrets scan~~ — done 2026-09-28. The stage criteria are now all evidenced; the owner may consider `scaffold-complete`.
4. ~~MVP engineering order: real LeRobot reader → rule-based validation/quarantine → deterministic content-addressed build → MCAP reader.~~ **All four done 2026-09-29**; every MVP criterion in the definition of done is now checked with evidence.
5. **Next stage is Production Baseline, and it starts with a number, not a feature.** Backlog B-019: compile the MCAP dimension layout once per topic instead of rebuilding path strings per message (EXP-0004 measured it at 51% of ingest), then re-measure and supersede EXP-0004 rather than editing it. Then the failure-mode catalog (`testing/failure-modes.md`) has no row left `planned` — the chaos harness that B-016's detection-accuracy figure depends on is the same work.

## 8. Open architectural questions

- **Answered 2026-09-29** for MCAP: `scripts/make_mcap_log.py` is a closed-form generator, so the fixture is versioned by the script's arguments and its SHA-256, and a network download is not in the measurement path. Still open for LeRobot at full episode size (the current fixture is the 203-frame tabular slice, not the video shards).
- What exact catalog migrations and transaction/outbox strategy are needed to reconcile artifact writes with database state?
- What retry/lease/cancel semantics and worker concurrency limits will be implemented first?
- Should runtime metrics initially remain JSONL files or move to a database/exporter once actual query needs exist? Follow ADR 0008 triggers; avoid adding a telemetry service speculatively.
- What machine/hardware profile and owner-approved run conditions define the first committed baseline?

## 9. Benchmarking status

Harness and schema v2 are implemented and tested; `synthetic-episode-ingest` measures a tiny synthetic episode using an in-memory catalog and the filesystem artifact store (3 warmups, 10 trials). It is **not** a database, API, MCAP, LeRobot, or production throughput benchmark. Its first baseline was measured and committed on 2026-09-28 with owner authorization ([§13](#13-baseline-runbook-owner-green-flag-required), [EXP-0001](experiments/0001-synthetic-ingest-baseline.md)); it is a regression tripwire, not a performance target. A named `WORKLOADS` registry (`--workload`) adds run-intelligence workloads — real LeRobot ingest, quality analysis, validation evaluation, metrics aggregation, API/UI latency — measured **one workload per process** ([ADR 0019](decisions/0019-benchmark-workload-isolation.md); batching in one process inflates p50 ~24× on Windows) and recorded with raw samples in [EXP-0002](experiments/0002-run-intelligence-workloads.md). No new baselines are committed without owner authorization. Backlog: [benchmark backlog](benchmarking/backlog.md); rules: [methodology](benchmarking/methodology.md); review: [scaffolding review](reviews/2026-09-28-scaffolding.md).

## 10. Testing status

Latest local gates (2026-09-29, `just ci`): format, lint, strict type-check over 58 modules, tests, hygiene, and OpenAPI drift all pass; **659 passed, 91.74% coverage** against the isolated Postgres cluster (22 integration tests run when `DE_DATABASE_URL` is set and skip cleanly without it), hygiene clean, lock current. A Starlette/httpx `TestClient` deprecation warning is non-blocking and deferred. Live DB verification, clean-clone validation, and the history-wide secrets scan are all done. Repeat all gates after any change.

## 11. Observability status

JSON logs and correlation IDs flow across the current API → persisted job → worker → logs path. Host telemetry snapshots CPU/RAM/disk/network with optional GPU fields; host fields become nullable on psutil/OS errors, with an AccessDenied test in the passing suite. **Runtime metrics are emitted** at the worker and ingest call sites — `jobs_queue_depth`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`, `jobs_failures_total`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`, `artifacts_written_bytes_total` — written to a JSONL sink at `DE_METRICS_PATH` (default `var/metrics/runtime.jsonl`). Labels stay low-cardinality; IDs appear only in logs. Sink failures never break the pipeline. API request latency and catalog query timing are instrumented the same way, and `observability/aggregate.py` reads the sink back into percentile summaries, time-bucketed series, and worker heartbeat ages for `GET /api/v1/metrics` and `/ui/metrics` ([ADR 0017](decisions/0017-runtime-metrics-aggregation.md)). Distributed tracing remains deferred per [ADR 0008](decisions/0008-observability.md); psutil is recorded in [ADR 0013](decisions/0013-cross-platform-resource-telemetry.md).

## 12. Important decisions and rejected alternatives

- Problem: robot episode data engine (ADR 0003); platform over model-specific optimization.
- PostgreSQL catalog + thin custom queue (ADR 0005); content-addressed local artifacts / ecosystem data formats (ADR 0006); build-thin lineage/run records (ADR 0007).
- Stdlib JSON logging, correlation IDs, file metric direction, defer OTel/Prometheus/Grafana until need (ADR 0008); FastAPI (ADR 0009); modular monolith + separate worker (ADR 0010); 70% coverage floor (ADR 0011); host-based development, containers optional (ADR 0012); psutil host telemetry (ADR 0013).
- No performance approach has been measured or adopted; there are no experiment-backed optimization claims. Alternatives and triggers are recorded in the linked ADRs and [experiment registry](experiments/registry.md).

## 13. Baseline runbook (owner green flag required)

**Status: executed 2026-09-28 with owner authorization.** Baseline written at commit `f7ffbeb` (clean tree); verify pass reported no regression. One defect was found and fixed in this runbook while executing it: the command originally wrote a SHA-named file, but `DEFAULT_BASELINE` in `benchmarks/harness.py` is the fixed path `synthetic-ingest-windows.json`, so a SHA-named file would never be read by bare `just bench` and step 4 would have failed. Always write the default path, or pass the same `--baseline` to both invocations.

Do not start until the owner authorizes a measured baseline. Preconditions: a committed, clean tree; the machine you
will measure on must stay idle; nothing else heavy may run during the measurement.

```bash
just ci                                  # 1. all gates green before measuring
git status --short                       # 2. must be empty (provenance records dirty state)
just bench --write-baseline             # 3. writes benchmarks/baselines/synthetic-ingest-windows.json (the fixed default)
just bench                               # 4. re-run: must now compare and report no regression
```

Then, before tagging:

1. Inspect the written baseline and the raw result under `benchmarks/results/`. Confirm provenance is complete:
   git commit, dirty flag, config hash, hardware, OS/Python/lockfile, seed, timestamp, workload version, background-load note.
2. Update [EXP-0001](experiments/0001-synthetic-ingest-baseline.md) with the actual numbers, the hardware profile, and
   honest caveats. Do not restate the number as a throughput or production claim — it is a synthetic local microbenchmark.
3. Update [the experiment registry](experiments/registry.md) to record the measured entry.
4. Tick the Benchmarking criterion in [the definition of done](spec/definition-of-done.md) with links to the baseline,
   EXP-0001, and the result file. Leave Handoff unticked until the clean-clone and secrets-scan items close.
5. Re-run `just ci`, then commit as `bench: record first measured baseline` and push.
6. Only after the Handoff criterion is also evidenced, tag `scaffold-complete`.

**Second follow-up, same day — three operator-reported defects, all with the same shape.** Each had been measured and "fixed" at least once before, and each earlier fix treated the symptom. **Sticky column headers overlapped the data they labelled** because `overflow-x: auto` forces `overflow-y` to compute to `auto`: the table plate was already a scrollport, so the header's `top` was measured from the top of the table, not the page (measured: `th.top=483` inside its own `tr.top=438`). Measuring the nav height more carefully — as the previous pass did — could only ever be a better estimate of the wrong number. The plate is now explicitly a bounded scroll region and the header pins to `0`, which deletes the nav coupling and the `ResizeObserver` with it. **The page flickered once a second** because the poller's change guard compared the response against `root.innerHTML`; the browser re-serialises the DOM, so that comparison is *never* equal, the guard never fired, and every poll rewrote the region and re-triggered the refresh cue. It now compares against the last payload applied — also cheaper, since it skips a full region serialisation per poll. **The loading panel drew over an already-loaded page** because it was in normal flow and pushed the finished page down the viewport; it is now an out-of-flow overlay, hidden as served, shown only while a navigation is genuinely in flight. A fourth defect surfaced while writing the regression test: `.de-sweep-panel { display: grid }` outranks the UA rule for the `hidden` attribute, so the panel was a full-viewport overlay on every settled page until the hide was restated at class specificity.

Measured after the fixes: 110 samples across 11 s of polling → **0 opacity dips, 0 navigations, constant node count**; header `th.top === tr.top` with 0 rows overlapping on every table page. Suite is at **604 passing**, 93% coverage; lint, mypy and hygiene clean; all 43 GET endpoints in the contract answer with no 5xx. The general lesson is the one worth keeping: **two of these three had a plausible fix already applied and shipped**, and neither the stylesheet nor a passing test suite would have caught either. They needed a browser and a ruler, and the measurement had to be verified before it was believed — `snap.mjs`'s "133 px header overflow" warning turned out to be the script's own arithmetic, not the page's (`overflowsBy: -1` when measured directly).
