# Review: scaffolding — 2026-09-28

- **Reviewer role:** reviewer
- **Commits reviewed:** `d336d33` → `6b5f86f` (slice, benchmark foundations, then the remediation commits `c1ff222`, `e9dd126`, `4500585`, `6b5f86f`)
- **Stage being accepted:** Scaffolding
- **Updated:** second pass after remediation of findings 2–6

## Checklist (mark ✅ / ❌ / n-a, with evidence)

- [✅] Architecture docs match the implemented slice; target behavior is labeled as future work in `architecture/components.md`, `data-flow.md`, and `implementation/vertical-slice.md`.
- [✅] Significant decisions have ADRs 0001–0013 indexed in `decisions/README.md`.
- [✅] The API subset is documented in `architecture/api.md`; unimplemented endpoints are called targets.
- [✅] No duplicated automation: the duplicate `hygiene.yml` was deleted; `ci.yml` is the single workflow (`4500585`).
- [✅] Dependencies are justified: psutil by ADR 0013; core/optional dependencies are in `pyproject.toml` and the technology matrix.
- [❌] Gates pass locally (91 passed, 3 skipped, 91.34% coverage) but a **clean clone was not verified**.
- [✅] Coverage gate remains 70% in `pyproject.toml` per ADR 0011; actual coverage is well above it.
- [✅] Failure-mode catalog distinguishes planned, partial, and covered scenarios in `testing/failure-modes.md`.
- [x] Benchmark acceptance is **complete**: schema/harness/statistics/provenance implemented and tested, and a measured baseline is committed with full provenance. `just bench` now compares successfully and exits 0 (finding 1).
- [✅] Observability matches conventions: structured logs, correlation IDs, host/GPU telemetry with nullable fallbacks, and **runtime metrics now emitted at their call sites** (finding 4).
- [❌] Full secrets audit incomplete: current-tree hygiene passes; no history-wide `git log -p` or gitleaks scan. No `.env` was opened.
- [❌] Definition of done not met: baseline, clean-clone review, and accepted handoff remain open.
- [✅] Docs identify implementation status, review findings, benchmark limitations, and next steps; repository hygiene passes.

## Findings

| # | Severity | Finding | Resolution | Status |
|---|---|---|---|---|
| 1 | blocker | No committed benchmark baseline. `just bench` could not compare on a clean clone. | **Resolved 2026-09-28** with owner authorization: baseline written at `f7ffbeb` on a clean tree, verification pass reports no regression. Executing the runbook also exposed a defect in it — it wrote a SHA-named file while `DEFAULT_BASELINE` is a fixed path, so step 4 would have failed; the runbook is corrected. | closed |
| 2 | major | Harness did not fail closed on failed trials; summaries and resource references were not cross-checked; warmup failures aborted with no result. | `f764f74`/`e9dd126`: status, indices, references, percentiles, mean/stddev, and failure count are validated; warmup failures persist as failed results with zero trials; failed results cannot produce a baseline. Tests cover each path. | **resolved** |
| 3 | major | Baseline comparison ignored hardware compatibility. | Exact CPU/logical-CPU/RAM/GPU/OS/disk match with field-level difference reporting. Note for later: exact matching may be too strict across equivalent machines; revisit with evidence. | **resolved** (strictness noted) |
| 4 | major | Host psutil errors propagated; metric primitives had no call sites, so the registry was documentation only. | Nullable host fields with tests; `RuntimeMetrics` wired to worker and ingest call sites emitting queue depth, queue/run time, stage duration, failures, and ingest counters, with a non-fatal sink guarantee. | **resolved** |
| 5 | major | Baselines were unvalidated, surfacing as bare `KeyError`/`ValidationError`. | Typed `BaselineDocument` + `load_baseline` raising `BaselineFormatError` naming the file, the offending field, and the fix; CLI reports it without a traceback. Six validation tests. | **resolved** |
| 6 | minor | `ci.yml` and `hygiene.yml` duplicated the same job. | `hygiene.yml` deleted; one canonical workflow. | **resolved** |
| 7 | minor | Docs implied phase 05/06 work or `just bench` were incomplete. | README, status, DoD, handoff, and review updated to match reality. | **resolved** |

## Newly raised by remediation

| # | Severity | Finding | Action | Status |
|---|---|---|---|---|
| 8 | minor | Hardware comparison is exact-match, so a baseline is only usable on a byte-identical machine profile. | Acceptable for the first baseline; revisit with measurement evidence before the harness gates CI. | accepted, noted |
| 9 | minor | A non-blocking Starlette/httpx `TestClient` deprecation warning appears in the suite. | Cosmetic; resolve when upstream guidance firms. | deferred |

## Verification limitations

- Latest local gate run: `just fmt && just lint && just typecheck && just test && just hygiene && uv lock --check` → **91 passed, 3 skipped, 91.34% coverage**, hygiene clean, lock current.
- Three PostgreSQL-dependent tests skip because `DE_DATABASE_URL` is unavailable; the live DB path is unverified. Do not inspect `.env`.
- No clean clone was created. No history-wide secret scan or gitleaks run.
- `just bench` was executed and produced a **valid schema-v2 result** (10 trials, full provenance) before failing at the expected baseline step. That run is a harness verification, **not** an accepted performance measurement, and no baseline was written.

## Verdict

**Accept with follow-ups — the tree is ready for the owner's benchmark green flag; the stage itself is not yet accepted.**

All seven original findings are resolved and gated. The single remaining blocker is an authorization decision, not an engineering gap: publishing a measured baseline. Because that baseline must be produced by a deliberate owner-approved run, the **Benchmarking** and **Handoff** criteria in [the definition of done](../spec/definition-of-done.md) stay unticked, the stage stays at Scaffolding, and `scaffold-complete` must not be tagged.

Residual gaps, none of which block the green flag: live-Postgres verification, clean-clone validation, and the history-wide secrets scan.
