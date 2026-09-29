# Integration Telldown Plan — Curated Build-Ready Layer + Failures Read View

**Status: executed 2026-09-29.** All three lanes are implemented, tested, measured, and blackbox-verified against a running server. Deviations from the plan are recorded in §9.
**Release boundary:** one versioned release, committed as schema → repository → API → UI → tests → benchmarks → docs, then blackbox smoke tested against the running server.

This is the *integration telldown* for the curated build-ready layer and the validation-failures read view. It exists because
the curated layer (Lane 1), the failures view (Lane 2), and the Run report section that already landed (Lane 3's visual-QA piece)
share a read model and must compose into a single coherent curation experience. The plan is the integration contract that the
three lanes must satisfy together, not three independent feature descriptions.

## 1. What the integrated surface must do

An operator who has just curated some episodes should be able to:

1. **Inspect failures** in one place: why episodes are quarantined, which reason codes dominate, which profiles are noisy,
   which formats fail most. Bounded, paginated, read-only. `GET /api/v1/failures` + `/failures/episodes`.
2. **Save a surviving set** as a named slice: declare a filter configuration declaratively, recompute membership at read time so
   the slice never goes stale. `POST/PATCH /api/v1/slices`, `GET /api/v1/slices/{id}`, `GET /api/v1/slices/{id}/manifest`.
3. **Export the curated set** with content identity and provenance so a downstream build (or a human) can consume it. This already
   exists as `GET /api/v1/episodes/export`; the slice read path reuses the same manifest body.

The three lanes are *one experience*, not three features: failures → slice → export is the intended curation loop. The integration
telldown is the check that the loop is wired.

## 2. What "rigorous" means here

Rigorous does **not** mean "big test suite." It means:

- **Contract-completeness:** every public endpoint has a visible contract shape through a Pydantic response model (or a documented
  plain-JSON shape where a model would be over the line), a 422 path for bad input, a 404 path for unknown ids, and a 200 path
  for the normal case. The OpenAPI contract is the canonical read of that shape, regenerated with `just api-contract-write` and
  checked in `just ci`.
- **Blackbox over stubs:** contract tests for the new read paths use a fake catalog wired into `create_app` the same way existing
  contract tests do (`tests/contract/test_*.py`), not internal mocks. That verifies routing, serialization, error handling, and the
  decorator/middleware surface end to end inside FastAPI, which is exactly what a user hits.
- **Integration over database mocks:** at least one integration test per new resource exercises the real schema and real Postgres,
  so the SQL is real and the composition is real. The shared dev cluster on port 55432 is used, with unique names/UUIDs per test to
  avoid cross-test pollution (the existing pattern in `tests/integration/test_real_dataset_ingest.py`).
- **No flaky state dependencies:** nothing asserts on "the third row" or "the last item" in a way that depends on other tests'
  ordering. Each test builds the world it needs or reads by identity.
- **Performance is measured, not asserted:** latency is recorded with isolated benchmark workloads (ADR 0019) and written into
  `agents/experiments/`; no regression gate without a committed baseline, and no baseline without the owner's green flag. This
  release records measurements; it does not commit baselines for the new paths.
- **Driver is Headless Chromium** for visual QA, with the `playwright` install as the only acceptable tool (`agents/testing/requirements.md`).
  Visual QA uses `page.screenshot()` comparisons and a local HTTP server (never `file://` page loads).

## 3. Lane 1 — Dataset slices (curated build-ready layer)

### Intent
Name a filter configuration once, reuse it, and let a build cite a slice id. A slice is *declarative membership*, not a snapshot:
recompute from the quality signals at read time so it stays correct as the underlying episodes change.

### API surface (in addition to the already-committed `GET /api/v1/episodes/export`)
- `GET /api/v1/slices` — list saved slices, newest first, bounded.
- `POST /api/v1/slices` — create a named slice from a filter config (`state`, `flag`, name, notes). Idempotent on name.
- `GET /api/v1/slices/{id}` — slice metadata + member count + last-updated.
- `GET /api/v1/slices/{id}/manifest` — same manifest body as `GET /api/v1/episodes/export`, but filtered to this slice's
  membership.
- `PATCH /api/v1/slices/{id}` — update name/notes/filter config (recompute membership next read).
- `DELETE /api/v1/slices/{id}` — remove a slice (members are recomputed, not deleted from episodes).

### Repository surface (new module `src/data_engine/catalog/slices.py`)
- `list_slices`, `get_slice`, `register_slice`, `update_slice`, `delete_slice`, `slice_manifest(id)`.
- Membership: a join over episodes matching the stored filter config; recomputed on read, never stored as a stale snapshot.
- Content identity flows through exactly as in `list_episodes`: `source_hash`, `artifact_hash`, `format`, `state`, plus quality
  columns when present.

### Why this is "curated build-ready"
A build needs: what episodes to include, their content identity, and a way to reproduce the selection later. The export manifest
gives the first two; a slice gives the third. Together they are a minimal, versionable curation primitive — no dataset-build stage
required yet, but the surface a build stage would consume exists.

### What counts as done for Lane 1
- Slice table + migrations (inline idempotent DDL, same family as the rest).
- Repository methods above, all wrapped by the existing `_timed_operation` loop (ADR 0017).
- API routes above with response models, 422/404 paths, and regenerated OpenAPI.
- Contract tests for every route shape.
- At least one integration test on the real DB that creates a slice from real episodes and reads the manifest back.
- `api-slice-manifest` benchmark workload, measured isolated.
- Docs: api.md rows, backlog rows for the slice surfaces, slice plan note in `run-intelligence-slice.md`, status.md, HANDOFF.md.

## 4. Lane 2 — Validation-failures read view

### Intent
One place to see what is failing, how often, and on which profiles — using exactly the reason codes and vocabulary the quarantine
workflow already established. Read-only. Composable: after seeing failures, an operator saves the surviving set as a slice and exports it.

### API surface
- `GET /api/v1/failures` — summary: reason-code counts across episodes, per-profile failure counts, per-format failure counts,
  total quarantined count, total evaluated count, last-updated. Bounded summary, no unbounded row explosion.
- `GET /api/v1/failures/episodes` — the quarantined episodes, with reason codes and violation summaries, ranked by a severity-ish
  ordering (e.g. by number of distinct reason codes then by verdict), paginated like other read paths.
- Optional `?reason_code=` filter on the episodes list.

### Repository surface
- `failure_summary()` — aggregates over `validation_results` joined to `episodes`; reason-code counts, per-profile, per-format,
  total quarantined, total evaluated, last-updated of any relevant result.
- `failing_episodes(*, limit, before, reason_code)` — the quarantined episode rows with their current result's reason codes /
  violations, paginated.

### What counts as done for Lane 2
- Two repository methods, both wrapped by `_timed_operation`.
- Two API routes with response models, 404-less (they are summaries, not by-id reads; 422 only for bad params), and regenerated
  OpenAPI.
- A `/ui/failures` page in the same style as the other instrument pages (reason-code chips, links to episodes, a small summary table).
- Contract tests for both shapes.
- One integration test that actually quarantines episodes under a profile and reads the failures summary back.
- `api-failures-summary` and `api-failing-episodes` benchmark workloads, measured isolated.

## 5. Lane 3 — Visual QA of the Run report section + regression capture

### Intent
The Run report section that already landed must render correctly across the four vendored themes, across empty/loaded states, and
across the curated-layer pages that touch quality (the failures page and the slice manifest page both show quality and verdict
chips). Visual QA here is the regression gate for CSS/spacing/theme interactions, captured as Headless Chromium screenshots in
`C:/tmp/shots/` (the house convention).

### What "visual QA" includes here
- The job detail page with Run report, across all four themes (`default`, `amber`, `vt220`, `greenphosphor`), with both an empty
  produced list and a loaded one.
- The failures page (Lane 2) and the slice manifest page (Lane 1), to confirm reason-code chips and verdict badges render
  consistently and don't overflow or collapse.
- Theme fallback behavior: an unknown theme falls back to default and a hostile `theme=` value is rejected — both should not break
  the page.
- No `file://` page loads; the server is started on a free port, navigated to with `http://127.0.0.1:<port>/...`, and screenshots
  are taken with `page.screenshot()`.

### What counts as done for Lane 3
- A Headless Chromium screenshot script in `scripts/visual_qa_run_report.py` that captures the relevant pages into
  `C:/tmp/shots/` and prints a pass/fail summary per page and theme.
- The script is run as part of the telldown and its output is recorded (not committed images, but the run's pass/fail summary and
  any regressions fixed).
- Any CSS/spacing defects it finds are fixed and the script re-run until the relevant pages are clean.

## 6. How the lanes compose into one release

The release boundary is a versioned state of the code plus its docs. Concretely:

- **Schema + repository** (L1 slices, L2 failures) land first, because they are the dependency for the API and UI.
- **API** (L1 slice routes, L2 failure routes, plus the already-committed export route) lands next.
- **UI** (L2 failures page, L1 slice/manifest pages if wired, L3 visual-QA fixes) lands after the API it depends on.
- **Tests** (contract + integration for every new resource) land with the code they test, same commit.
- **Benchmarks** land with the code, measurements recorded in `agents/experiments/`, no baselines committed.
- **Docs** (api.md, backlog, slice plan, status.md, HANDOFF.md, telldown-plan.md itself) land with the code.

The order above is the commit order. Each commit is one logical change and `just ci` green before it is pushed. The release is
*versioned* by the final commit's position on `main` — there is no release tag yet, but the state is a coherent, tested, documented
cut.

## 7. Blackbox smoke test (after the release is committed and pushed)

After the three lanes are committed and pushed, a Headless Chromium smoke test exercises the running server, not the stubs:

1. Start the server on a free port via `just run` (or the dev runner), wait for `/health` to return `ok`.
2. Navigate to the curated-layer pages: `/ui/failures`, `/ui/slices` (if wired), a job detail page with a Run report, the episodes
   page with a flag filter, the insights page.
3. Switch themes via `?theme=` across all four themes and confirm no layout collapse or broken glyphs.
4. Hit the API endpoints directly from the smoke script (the same Headless Chromium instance, or a lightweight Python client) and
   confirm the canonical 200/404/422 shapes and the new fields (membership, reason codes, manifest identity fields) are present in
   the JSON.
5. Capture screenshots into `C:/tmp/shots/` for the curated pages at each theme and note any regressions.

The smoke test is the final "does the real server behave" gate, after the unit/contract/integration gates have passed in CI.

## 8. Why not more, and why not less

More would mean building the dataset-build stage, Parquet exports, MCAP, or the ML monitoring layer — all explicitly deferred by this
release's boundary and by the platform's thesis (the platform is the product; models are workloads). Less would mean skipping the
integration DB tests or the visual QA, which is exactly the rigor this release is being asked for. The plan is deliberately bounded
to what makes the curated layer and failures view real, testable, measurable, and visual-regression-safe.

## 9. Execution record (what actually shipped)

**Lane 1 — slices.** `episode_slices` + `slice_memberships` tables; repository `list_slices`/`get_slice`/
`register_slice`/`update_slice`/`delete_slice`/`slice_manifest`; six routes (`POST/GET /api/v1/slices`,
`GET/PATCH/DELETE /api/v1/slices/{id}`, `GET /api/v1/slices/{id}/manifest`); `/ui/slices` page.
Membership recomputes on read. Duplicate names raise a new `SliceNameConflict` → 409 `SLICE_NAME_CONFLICT`
rather than borrowing the idempotency-key wording, which would have been misleading.
**Lane 2 — failures view.** `failure_summary` + `failing_episodes`; `GET /api/v1/failures` and
`/failures/episodes`; `/ui/failures` page with reason-code chips and per-episode verdicts.
**Lane 3 — visual QA.** All four themes screenshotted for both new pages into `C:/tmp/shots/`; hostile
`theme=` falls back to the default; data renders (3 quarantined episodes, 2 slices) and escaping holds.

**One real bug the blackbox pass caught, which the stub contract test and the first integration test
both missed:** `slice_manifest` hardcoded `count: 0` while returning real items, so a client reading
`count` saw an empty slice. Found by driving the live server over HTTP; fixed by deriving `count` from
the page it ships, and pinned with `assert manifest["count"] == len(manifest["items"])`. This is the
concrete argument for the blackbox gate: neither the stub nor the item-level assertions noticed it.

**Shared vocabulary.** `src/data_engine/curation.py` now owns the episode states, flags, reason codes,
and the `episode_predicates` SQL builder, so the catalog listing, the export manifest, the API
validation, and a saved slice cannot drift apart. The catalog no longer imports from the web layer.

**Measurements (isolated, ADR 0019; EXP-0002 addendum; no baselines written).**
`api-failures-summary` p50 5.53 / p95 7.10 ms; `api-failing-episodes` p50 7.00 / p95 9.49 ms;
`api-slice-manifest` p50 6.99 / p95 8.63 ms — all n=10, 0 failures, in the same band as the existing
read endpoints they reuse.

**Suite at release:** 331 passing, 93.17% coverage, hygiene clean, OpenAPI contract committed.
