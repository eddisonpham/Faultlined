# Review: scaffolding — 2026-09-28

- **Reviewer role:** reviewer
- **Commit reviewed:** `d336d33` (vertical slice + benchmark foundations) plus the benchmark-hardening follow-up commit
- **Stage being accepted:** Scaffolding

## Checklist (mark ✅ / ❌ / n-a, with evidence)

- [✅] Architecture docs match the implemented slice; target behavior is mostly labeled as future work in `architecture/components.md`, `architecture/data-flow.md`, and `implementation/vertical-slice.md`.
- [✅] Significant decisions have ADRs 0001–0013 indexed in `decisions/README.md`.
- [✅] The API subset is documented in `architecture/api.md`; unimplemented endpoints are called targets.
- [❌] No duplication: `.github/workflows/ci.yml` and `hygiene.yml` run identical quality jobs (finding 6). Runtime metric primitives are not wired (finding 4).
- [✅] Dependencies are justified: psutil by ADR 0013; core/optional dependencies are in `pyproject.toml` and the technology matrix.
- [❌] Gates pass in the current working tree (see verification below), but a clean clone was not created or tested.
- [✅] Coverage gate remains 70% in `pyproject.toml` per ADR 0011; latest local suite reports 92.04%.
- [✅] Failure-mode catalog accurately distinguishes planned, partial, and covered scenarios in `testing/failure-modes.md`.
- [❌] Benchmark acceptance remains incomplete: no baseline or measured experiment exists, and `just bench` cannot compare from a clean clone without a baseline (finding 1).
- [❌] Observability is incomplete: host telemetry has an initial nullable fallback, but runtime metrics are not wired (finding 4).
- [❌] Full secrets audit is incomplete: current-tree hygiene passed, but no history-wide `git log -p` or gitleaks scan was performed. No `.env` was opened.
- [❌] Definition of done is not met: benchmark baseline, complete observability, clean-clone review and accepted handoff criteria remain open.
- [✅] Current docs identify implementation status, review findings, benchmark limitations, and next steps; links pass repository hygiene.

## Findings

| # | Severity (blocker/major/minor) | Finding | Action | Status |
|---|---|---|---|---|
| 1 | blocker | No committed benchmark baseline exists. `just bench` writes an ignored result then fails comparison against the absent default baseline. No baseline was created because measured-baseline publication has not been authorized. | Obtain owner authorization, run the workload under reviewed conditions, document provenance/caveats, record the experiment, commit the baseline, and verify comparison from a clean clone. | open |
| 2 | major | The initial harness did not fail closed on failed trials and did not cross-check summary/resource references against raw trials. | Follow-up validates trial status, indices, resource references, percentiles, mean/stddev, failure count, refuses failed baseline writes/comparisons, and persists warmup failures as failed results with zero measured trials. Tests cover failed operation, warmup persistence, inconsistent summary, and published-schema parity. Full local gates pass. Bootstrap CI consistency and CLI-exit tests remain. | partially addressed; follow-up open |
| 3 | major | Baseline comparison initially ignored hardware profile compatibility and baselines had no fully validated schema. | Comparison now checks exact CPU, logical CPU count, RAM, GPU descriptor, OS, and disk equality, with a mismatch test. Local gates pass; baseline input validation/schema-level tests remain. Exact matching may be overly strict across equivalent machines and should be revisited with evidence. | partially addressed; follow-up open |
| 4 | major | Host psutil errors initially propagated; metric primitives and `JsonlMetricSink` have no runtime call sites, so the metric registry is not emitted runtime telemetry. | Each host field is now nullable on psutil/OS errors, with an AccessDenied test; local gates pass. Runtime metric call sites remain absent and must either be implemented or kept explicitly deferred. | partially addressed; follow-up open |
| 5 | major | Baseline structure is not validated with a shared contract; comparison can still raise low-context key/type errors for malformed baseline JSON. | Add baseline schema validation and improve malformed-baseline errors before treating comparison verdicts as regression gates. | open |
| 6 | minor | `.github/workflows/ci.yml` and `.github/workflows/hygiene.yml` duplicate the same quality job on push and pull request. | Consolidate to one workflow or assign distinct responsibilities. | open |
| 7 | minor | README/status docs implied phase 05/06 work or `just bench` were still being completed, despite implemented foundations; baseline and DB limitations were not prominent. | README, implementation status, DoD, and handoff now identify the implemented subset and limitations. | addressed |

## Verification limitations

- Latest gate run after warmup/schema-parity follow-up: `just fmt && just lint && just typecheck && just test && just hygiene && uv lock --check`; 76 passed, 3 skipped, 92.04% coverage, hygiene clean, lock current. One Starlette/httpx deprecation warning remains.
- Three PostgreSQL-dependent tests skipped because `DE_DATABASE_URL` is unavailable; no live DB verification was performed. Do not inspect `.env`.
- No clean clone was created. No history-wide secret scan or gitleaks scan was performed.
- No benchmark baseline or result was published; there are no performance claims based on measurements.

## Verdict

**Reject for stage acceptance; continue with follow-ups.** The implementation is a useful synthetic scaffolding slice, and local quality gates pass, but the committed benchmark baseline requirement remains deliberately unmet. Runtime metric instrumentation, baseline validation, live-Postgres verification, clean-clone validation, full secret audit, and duplicate CI workflow remain open. Schema parity and warmup-failure persistence are addressed and pass local gates. Do not advance the stage or tag `scaffold-complete` until blockers are resolved and a reviewer accepts a repeated review.
