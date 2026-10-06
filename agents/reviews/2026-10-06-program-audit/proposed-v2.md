# Proposed v2 — what to build next, in order

- **Date:** 2026-10-06 · **Tree:** `693edb3` · Companions: [technical-debt.md](technical-debt.md),
  [missing-production-capabilities.md](missing-production-capabilities.md),
  [improvement-ranking.md](improvement-ranking.md).
- Rules this plan obeys: ADR before architecture change (never silently edit history — supersede or
  amend deliberately), smallest useful change, measure before optimizing, no feature copied from other
  products without a motivation found in *this* codebase or its measurements. Every phase states its
  acceptance criteria and what would falsify it.

## P0 — make what exists tell the truth (no design decisions needed)

The engine currently misreports data quality, misreports failures, and ships a monitor that never runs.
Nothing else matters until these are closed.

1. **Fix the topic-name quality defect (T-01).** Match the gripper exclusion against dimension *roles*,
   not name substrings of `<topic>.<path>`. Acceptance: the EXP-0014 D1 isolation fixture pair (same
   payload, `/joint_states` vs `/left/gripper/joint_states`) yields identical verdicts; the
   `grip`-naming regression is a unit test in `tests/unit/test_quality.py`.
2. **Fix the non-finite ingest failure (T-02).** Sanitize or reject non-finite frame values at the reader
   boundary (ADR 0023 already sets the guarantee in `analyze()`); deterministic input errors must be
   **terminal** with a precise reason code, never retried 3×. Acceptance: EXP-0014 D2 fixture completes
   in one attempt with a clear verdict or a terminal refusal naming the field.
3. **Surface real failure reasons (T-03/T-14).** Persist `type(exc).__name__` + message (sanitized) on the
   job row and render it in `job_report` / `/ui/jobs/{id}`. Acceptance: a failing reader shows its own
   error text in the job report; `"job handler failed"` survives only as the last-resort fallback.
4. **Schedule the monitor (T-04).** Smallest change: call `MonitorService.tick()` from the worker loop
   (or a `de monitor` loop alongside `de dev`) on an interval. **ADR 0031 required** (how monitoring is
   scheduled; amends ADR 0020's HTTP-triggered shape). Acceptance: an incident appears on `/ui/incidents`
   in a deployment where nobody POSTs `/api/v1/monitoring/tick`; tick cadence and cost are recorded as an
   experiment (baseline: 349–407 ms p50, EXP-0016).

## P1 — complete the operator's core loop (decides whether this is a product)

5. **Browser routes for validate / build / export / slice creation (ranking item 2).** Extend
   `ui_submit_job`'s "same Pydantic model as the JSON API" pattern to the four missing job kinds and
   slice creation. ADR required only if the form-parsing approach changes (see P2 item 9); otherwise this
   is scope addition within ADR 0014. Acceptance: an operator completes ingest → validate → build →
   export → slice without leaving the browser; EXP-0017's inventory script shows the gap closed; every
   new form keeps the typed-values-preserved error path.
6. **Batch vocabulary triage (T-08).** One form, many strings: multi-select accept/map/dismiss with a
   single POST, then land on the refreshed queue with the next item focused. Acceptance: triaging 10
   strings is ≤ 2 page loads and ≤ 3 clicks (vs today's 10 loads / 10 clicks / ~6 s of waiting,
   EXP-0017).
7. **Kill the duplicated work on `/ui/vocabulary` (T-06).** Either give `vocabulary_health` a mode that
   reuses the page's already-fetched rows, or have the page consume only `vocabulary_health`'s outputs.
   Acceptance: page p95 at 10k < 300 ms (from 595 ms) and the EXP-0015 decomposition shows no query run
   twice.

## P2 — performance and shape (measurable, no product decisions)

8. **Bound the aggregate endpoints (T-07).** `quality/summary` (399 ms / 2.2 MiB) and `/ui/insights`
   (5.2 MiB) need either pre-aggregation (ADR 0032 rollups, planned B3) or capped/paginated responses.
   Measure before choosing: if a rollup table is the answer, it needs the P0-4 scheduling primitive.
   Acceptance: both endpoints < 200 ms p95 and < 200 KiB at 10k episodes.
9. **Extract route modules from the monoliths (T-09/T-10).** Split `api/app.py` by surface family (jobs,
   vocabulary, monitoring, downloads) and `web/pages.py` by page family, keeping behavior identical
   (OpenAPI contract and ui-audit must not change). If form parsing grows past two more forms, adopt
   `python-multipart` in an ADR that amends ADR 0014's dependency freeze — a one-dependency amendment
   beats five hand parsers.
10. **Hosted CI (T-11).** A workflow that runs `just ci` against a service Postgres. Acceptance: a PR
    cannot merge red.

## P3 — platform boundaries

11. **Format adapters at the ingest boundary (missing-production §3).** The one place money/compute is
    justified (P3 in the [2026-10-04 verdict](../2026-10-04-engineer-assessment-verdict.md)): ROS 2
    sqlite3 bag and CDR/protobuf MCAP decoding first (they are the two shapes EXP-0014 proved are
    common and mishandled), each behind `ingest/readers/registry.py`. Acceptance: the EXP-0014 corpus
    fixtures for those formats ingest with real decoded channels and a real quality verdict.
12. **Garbage collection (T-13).** `de gc` for orphaned blobs, `.pending-*` residue, superseded exports,
    old metrics JSONL. Acceptance: run after a crash drill leaves the store byte-identical to a clean run
    modulo live content.
13. **Incident egress (webhooks, B2).** ADR 0031 should carry this: deterministic notifier classification
    already exists (ADR 0020); delivery is the missing half. Acceptance: an injected fault produces a
    delivered notification with the same determinism the classify step has.
14. **Build diff (B5)** and vocabulary/decision-log export. Acceptance: diff of two builds answers
    "which episodes entered/left and why" from stored data alone.

## P4 — the bets to settle with measurement, not opinions

15. **Run the vocabulary falsifier gates** (plan §4): unmapped share ≤ 20 % and candidate confirm
    precision ≥ 50 % on a **real external corpus** (not vendored fixtures). If either falsifies, the
    vocabulary-first bet needs a rethink before more UI is built on it. This is the highest-information
    experiment in the program and costs a corpus plus one run of `ranker.py::candidates`.
16. **Monitor detection accuracy** (B-016): the full chaos campaign with clean-replicate null bands;
    precision/recall figures or an explicit "unmeasured" line in every doc (current state).
17. **Consider integration events → audit identity** only when a second human exists (missing-production
    §1). Not before — it is speculative for a local-first tool.

## Explicit non-goals for v2

- No model inference in decision paths (ADR 0020), no quantization/TensorRT scope (CLAUDE.md guard).
- No SPA rewrite; ADR 0014's architecture stands unless P1 item 5 proves it cannot carry the mutation
  surface — and if it can't, the failure will show up as form-parsing sprawl in `ui_submit_job`, which is
  the trigger condition to revisit.
- No multi-tenancy, no object storage, no HA until a real deployment demands them.

## Sequencing summary

| Phase | Theme | Gate to exit |
|---|---|---|
| P0 | truthfulness | EXP-0014 defects closed with regression tests; monitor fires without manual tick |
| P1 | complete loop | full loop in-browser; triage ≤ 3 clicks/10 strings; `/ui/vocabulary` < 300 ms |
| P2 | performance & shape | aggregates < 200 ms/200 KiB @10k; CI hosted; monoliths split with unchanged contract |
| P3 | boundaries | foreign formats decode; gc exists; incidents deliver; build diff answers |
| P4 | falsifiers | vocabulary bet validated or explicitly revised; monitor accuracy quantified |
