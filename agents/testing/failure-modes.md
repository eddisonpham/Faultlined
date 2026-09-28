# Failure-Mode Catalog (test-facing)

Drafted in phase 03; mirrors [../architecture/failure-handling.md](../architecture/failure-handling.md) (source of
detection/recovery design). Owned by test-engineer: every row gains a **real test reference** as stages land; the
"Covered by" column is filled in during phases 04–05 and audited at each stage gate. Test strategy:
[testing-standards.md](testing-standards.md). Requirements trace: [../spec/requirements.md](../spec/requirements.md) §3.

Status legend: `planned` (test named, not written) → `covered` (test exists, referenced) → `hardened` (also exercised
in fault-injection/load stages).

| ID | Failure mode | Component | Planned test (type / location) | Stage target | Covered by |
|---|---|---|---|---|---|
| F1 | Corrupt/truncated MCAP; malformed LeRobot dir | ingest | unit: reader fixtures (`tests/unit/readers/`); integration: submit corrupt file (`tests/integration/test_ingest_failures.py`) | MVP | planned |
| F2 | Missing channel / bad timestamps / NaN | validation | unit: per-rule tests (`tests/unit/rules/`); integration: quarantine + reason codes | MVP | planned |
| F3 | Invalid validation profile / config | validation, api | contract: 4xx problem codes (`tests/contract/`) | MVP | planned |
| F4 | Duplicate job submission (idempotency) | jobs, api | unit: key table; contract: double POST replay + 409 conflict | MVP | planned |
| F5 | Worker crash / kill mid-job | jobs, worker | integration: kill worker between claim and completion; assert requeue + at-least-once absorption | Baseline | planned |
| F6 | Stage timeout (slow handler) | jobs | integration: handler past deadline → `timed_out`; late completion discarded | MVP | planned |
| F7 | Cancellation races (mid-run, mid-retry, after completion) | jobs | unit: cancel token; integration: cancel mid-build; assert documented race outcome | MVP | planned |
| F8 | Partial pipeline (ingest ok, index fails) | jobs chaining | integration: inject index failure → heal → child completes | Baseline | planned |
| F9 | Disk full / artifact write failure | storage | integration: injected write error (tmpdir quota/monkeypatch) → clean `failed`, no dangling row | Baseline | planned |
| F10 | Postgres down mid-request/job | catalog, api | integration: stop/start DB → 503 + retry/backoff, no state loss | Baseline | planned |
| F11 | Restart with in-flight jobs (NFR-006) | jobs | integration: restart mid-job → lease sweep requeues | MVP | planned |
| F12 | GPU OOM / GPU absent (NFR-007) | workloads, observability | unit: workload raising OOM; CI: no-GPU admission path (`gpu_required` rejected, telemetry degrades) | MVP | planned |
| F13 | Telemetry/log sink failure | observability | unit: broken sink during stage → pipeline unaffected | MVP | planned |
| F14 | Build nondeterminism (NFR-004) | builds | CI test: build → clean → rebuild → identical manifest hash + directory hashes | MVP | planned |
| F15 | Orphan blobs / stale staging dirs | storage | integration: kill build mid-materialize → `gc` removes orphans | Baseline | planned |
| F16 | Lineage inconsistency | catalog | unit: rollback leaves no dangling edge; audit query in `doctor` | Baseline | planned |

Additional classes required by [../spec/00-original-specification.md](../spec/00-original-specification.md) §6 to be
disaggregated here as implementation reveals them: network failure (S3-tier trigger era), duplicate *data* (same
episode ingested from two sources — distinct from F4), invalid API pagination/cursor misuse.

Audit rule: a stage gate may not claim "failure-mode catalog covered by tests" (definition-of-done stage 3) while any
row is `planned`.
