# Implementation Plan — vocabulary-first task curation + operator-convenience extensions

**Status: planned 2026-10-04.** Implements the two research verdicts:
[clustering-methods-verdict.md](../research/clustering-methods-verdict.md) (Option D) and
[infrastructure-gaps.md](../research/infrastructure-gaps.md) (P1–P5). This is the *how*; the
research docs own the *what and why*. Deviations from this plan get recorded here (§9-style) when
execution starts.

## 0. Ground rules (every slice)

- **ADR before code.** New ADRs: `0029-task-vocabulary-first.md`, `0030-streamed-downloads.md`,
  `0031-alert-delivery-webhooks.md`, `0032-metric-retention-rollups.md`. Nothing else is an
  architecture change; P4/P5 are pure reads and presentation, documented in
  [api.md](../architecture/api.md) instead.
- **Green before commit:** `just ci` per slice (lint, strict mypy, tests, 70% coverage floor,
  hygiene, OpenAPI drift). Route changes require `just api-contract-write` in the same commit.
  UI slices additionally run `just ui-audit`. Integration tests need `just pg-up` +
  `DE_DATABASE_URL`.
- **Constraints that do not bend:** ADR 0014 (no new runtime dependencies — stdlib only),
  ADR 0020 (no model/LLM anywhere in the decision path; nothing auto-labels), ADR 0017 (the
  JSONL sink stays the source of truth), ADR 0026 (confirmation-as-task-strings and the >50%
  orphan rule survive the rework), ADR 0028 (every schema change is a forward-only migration,
  baseline kept in sync for fresh installs).
- **Evidence:** measured claims land in `agents/experiments/` records (next free numbers). No
  committed benchmark baselines without the owner's green flag ([HANDOFF §13](../HANDOFF.md)).
- Docs (status.md, HANDOFF, api.md, repo-layout) update in the same commit as the change.

## 1. Track A — vocabulary-first task curation

Restated feature (verdict §4): the product is a **curated task vocabulary** — stable entry IDs,
human-approved preferred labels, task strings mapped onto them. Clustering is demoted to a
synonym-candidate ranker. The human queue shrinks from "name every cluster forever" to "decide
novel strings".

### A1 — ADR 0029 + schema (first real migration)

- `agents/decisions/0029-task-vocabulary-first.md`. Supersedes ADR 0026's *pipeline shape*
  (proposal-as-primary-object) while keeping its confirmation/orphan semantics; states that
  explicitly rather than editing ADR 0026.
- Tables in `catalog/database.py` baseline **and** as migration `0002` in
  `catalog/migrations/` (REGISTRY):
  - `task_vocabulary_entries` (id, preferred_label, notes, created_at)
  - `task_vocabulary_mappings` (task_string PRIMARY KEY, entry_id, provenance
    `ingest|confirm|merge`, mapped_at)
  - `task_vocabulary_events` (merge/split/label-change history, so merges are explicit and
    reversible)
- Data migration backfills entries + mappings from `cluster_confirmations` (provenance
  `confirm`); a fresh install gets the same rows from the baseline path.
- Tests: migration runs once through the ADR 0028 runner API, `de migrate --status` clean
  before/after.

### A2 — vocabulary store

- New `src/data_engine/catalog/vocabulary.py`, patterned on `catalog/slices.py`: every method
  wrapped in `_timed_operation` (ADR 0017).
- Surface: `map_or_enqueue(task_string)`, `list_unmapped(limit, before)` (novelty-ranked),
  `suggest_candidates(task_strings)` (ranker output), `confirm_mapping`, `merge_entries`,
  `split_entry`, `vocabulary_health()` (unmapped share as headline, plus the existing verb
  rate / distinct cores / orphaned count).
- Merge/split write `task_vocabulary_events` and apply the ADR 0026 §4 >50% orphan rule to
  mappings.
- Tests: unit + one integration test per resource on real Postgres (unique ids per test).

### A3 — ingest wiring (map-or-enqueue)

- `ingest/service.py` (`EpisodeIngestService.ingest` / `ingest_path`) calls `map_or_enqueue`
  for each episode's task string. Episode rows keep the raw `task_string` unchanged — the
  mapping is a view, mirroring ADR 0026 §3.
- Tests: mapping round-trip; unmapped-queue priority order; re-ingest idempotence (F26
  duplicate class — re-ingesting must not duplicate queue rows).

### A4 — candidate ranker (clustering demoted)

- `clustering/` shrinks: `extract.py` kept byte-for-byte (it is the measured part — 48/48 on
  the study corpus); new `clustering/ranker.py` answers "do these unmapped strings belong to
  entry X, or form one new entry?". `online.py` centroids stay only if the ranker needs them;
  `proposals.py`'s proposal-as-object machinery is retired.
- Label suggestion is deterministic first: the **majority core** of the candidate group (zero
  dependencies, reproducible). The optional offline LLM proposer is **not built in this plan** —
  it waits on the candidate-precision falsifier (verdict §4), and if built is a non-authoritative
  offline CLI behind the same confirm gate.
- Tests: ranker determinism (same input → identical ordering); a measurement recorded as an
  experiment record using EXP-2.5-style pairs drawn from the real unmapped queue.

### A5 — API + UI surface

- Routes `GET/POST /api/v1/vocabulary...` in `api/app.py`; `/ui/vocabulary` in `web/` (the
  unmapped queue, synonym candidates per entry, merge/split actions, health tiles). Same
  server-rendered / no-JS constraints as ADR 0026 §6.
- `/ui/clusters` answers a redirect to `/ui/vocabulary` so old links die honestly; nav and
  keyboard shortcuts updated. OpenAPI regenerated; contract tests in
  `tests/contract/test_cluster_api.py` retargeted (updated deliberately, not silently).
- `just ui-audit` green across four themes.

### A6 — cutover and teardown

- The rebuild path stops deleting anything (rebuild idempotence is the ADR 0026 §3 bug class —
  pinned by test: rebuild twice, mappings and entries byte-identical).
- `catalog/clusters.py` trimmed to what the ranker/health paths still read; old tables
  (`task_clusters`, `task_cluster_members`, `cluster_review_decisions`, `cluster_runs`) kept as
  read-only archive for one release, dropped by a later forward-only migration (owner decision
  in §5).
- Docs: api.md rows, status.md, HANDOFF, repo-layout, definition-of-done note. Falsifier
  measurement recorded: **unmapped share over a real corpus** (verdict §4: >20% means extraction
  is the bottleneck — do lexicon work before anything else).

## 2. Track B — operator-convenience extensions (P1–P5)

### B1 — Downloads (P2, closes G2)

- ADR 0030: an export is **the same read with a different serializer** — no materialized export
  artifacts, no export service.
- Shared streaming serializer (new module, e.g. `api/download.py`): `csv` via stdlib `csv`,
  `jsonl` via per-row `json.dumps`, rows streamed from the existing catalog cursors — never
  materialize-then-send. `?format=csv|jsonl` + `Content-Disposition` on the list routes
  (`/api/v1/jobs`, `/episodes`, `/artifacts`, `/failures/episodes`, `/incidents`, `/contracts`,
  `/metrics` series).
- UI: a "Download" link per table reusing the exact query string the table was rendered with —
  what you see is what you get. Parquet stays the domain of the existing LeRobot-v3 export
  (ADR 0025); no new Parquet writer.
- Tests: row-count parity between view and file; generator-only streaming (assert no list
  materialization); column-order stability (a CSV is a contract).

### B2 — Alert delivery (P3, closes G3)

- ADR 0031 (amends ADR 0020 §10.3 deliberately, not silently): delivery is a signed webhook —
  stdlib `urllib` + `hmac`, Slack-compatible payload shaped like `notify_preview()`'s output,
  plus a daily digest mode honoring `notify_class` and the existing alert budget.
- `DE_NOTIFY_WEBHOOK` absent = dry-run exactly as today. Secret handling per ADR 0002: env
  only, never logged or committed.
- New `src/data_engine/monitoring/delivery.py`; wired where `notify_preview()` is rendered
  today. A failed delivery never blocks the monitor tick (the ADR 0008 lesson) and suppressed
  incidents stay suppressed.
- Tests: signature verification round-trip; delivery failure isolation; digest grouping;
  local-HTTP-server test (the fetcher's no-egress pattern).

### B3 — Retention-tier rollups (P1, closes G1, enables G5/G6)

- ADR 0032 (amends ADR 0017): the JSONL sink stays the hot tier and the source of truth; a
  compaction step folds records older than N days into `var/metrics/rollups/YYYY-MM-DD.jsonl`
  holding the p50/p95/p99 + bucketed means `aggregate.py` already computes. No metrics database
  (the NOT-list stays).
- `src/data_engine/observability/rollups.py` reusing `summarize()`/`series()` unchanged; a
  `de` CLI compaction command (crash-safe: write temp file, atomic rename). Reads: window < 24h
  → tail; longer → tail + rollups merged. `/api/v1/metrics` gains `since`/`until`.
- Tests: rollup idempotence, tail/rollup merge correctness at boundaries, empty-day handling,
  mid-compaction crash safety.

### B4 — History-aware presentation (P5, closes G5/G6; depends on B3)

- Time-range picker (a form — no JS) on `/ui/metrics` and `/ui/status`; incident and
  vocabulary events annotated on the charts (`web/charts.py` already draws from series data);
  `/ui/health` page stacking validation rate, quarantine rate, queue depth, incident counts
  over the rollups.
- Tests: `web/` unit tests + `just ui-audit`.

### B5 — Build diff (P4, closes G4; independent, lands anytime)

- `GET /api/v1/builds/{a}/diff/{b}` + `/ui/builds/{a}/diff/{b}`: episodes added/removed,
  profile-hash change, quality-stat deltas. Pure function over two stored manifests
  (`builds/diff.py`); no new writes.
- Tests: pure-function unit tests over crafted manifest pairs + contract routes.

## 3. Sequencing and release boundaries

| Phase | Content | Why here |
|---|---|---|
| 0 | Commit the two research docs + this plan | docs-only commit; knowledge in the repo before code |
| 1 | B1 (P2 downloads) | cheapest win, no schema, no ADR dependencies; restores operator trust immediately |
| 2 | Track A (A1→A6) | the headline verdict; several sessions, each slice independently green |
| 3 | B2 (P3 webhooks) | closes the most operationally dangerous gap |
| 4 | B3 → B4 (P1 → P5) | B4 reads B3's rollups; do not reorder |
| 5 | B5 (P4 diff) | independent; slot it wherever a small slice is wanted |

Each phase ends `just ci` green; each commit is one logical change (conventional commits). If the
clustering pain outranks downloads for the operator, swap phases 1 and 2 — nothing in either
depends on the other.

## 4. Verification and falsifier gates

- Per slice: `just ci`; per UI slice `just ui-audit`; integration slices on `just pg-up`.
- Track A gates (verdict §4): unmapped share >20% after seeding from one real corpus → stop and
  do lexicon work first; synonym-candidate confirm precision <50% → re-measure with EXP-2.5-style
  pairs before building anything else.
- Track B gates (infrastructure-gaps §5): export usage checked via `api_requests_total` by route
  after B1; rollup query latency/lossiness on a 30-day drill after B3 (slow or lossy → only then
  evaluate an embedded columnar read cache); suppressed/total ratio rising after B2 → tune the
  budget policy before adding any channel.

## 5. Owner decision points

1. **Old cluster tables:** archive-then-drop (recommended) vs drop in the cutover migration.
2. **LLM label proposer:** deferred in this plan; build only after the candidate-precision
   measurement, offline and non-authoritative if at all.
3. **Phase order swap** (§3) if downloads are not the top operator pain.

## 6. Execution record

Running log as slices land; deviations from §1-§5 are stated, not hidden.

- **B1 (downloads) — shipped 2026-10-04.** `api/download.py` shared streaming serializer;
  `?format=csv|jsonl` on jobs/episodes/artifacts/failures/episodes/incidents/contracts/metrics;
  UI Download links on the six table pages; `catalog.list_episodes` gained a `before` cursor
  (the flag views' signal ranking cannot page consistently, so downloads order newest-first
  through the cursor path — membership identical, order differs). ADR 0030. Bound at 50,000
  rows per download; the first row is pulled before streaming so catalog failures stay JSON
  errors. No ADR deviation beyond what ADR 0030 records.

## 7. Not built (both NOT-lists, condensed)

No BERTopic/embedding cluster stack; no LLM in the grouping or decision path; no auto-labeling;
no metrics database or scraper; no OpenLineage/Marquez stack; no email-first delivery; no export
service or materialized export artifacts.
