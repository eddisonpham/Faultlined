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
| F3 | Invalid validation profile / config | validation, api | contract: 4xx problem codes (`tests/contract/test_ingest_api.py::test_ingest_rejects_misaligned_samples_without_database`) | MVP | covered (invalid episode shape; profile validation remains planned) |
| F4 | Duplicate job submission (idempotency) | jobs, api | unit: key table; contract: double POST replay + 409 conflict | MVP | partial: `tests/unit/test_catalog_repository.py` (repo behavior) + `tests/contract/test_ingest_api.py::test_job_submission_uses_public_contract_and_correlation_header` and `tests/integration/test_job_lifecycle.py::test_submit_job_records_retry_budget_and_deadline`; the retry policy also participates in the request hash |
| F5 | Worker crash / kill mid-job | jobs, worker | integration: kill worker between claim and completion; assert requeue + at-least-once absorption | Baseline | planned |
| F6 | Stage timeout (slow handler) | jobs | integration: handler past deadline → `timed_out`; late completion discarded | MVP | covered (ADR 0015): `tests/integration/test_job_lifecycle.py::test_reaper_times_out_expired_deadlines_only` and `::test_worker_times_out_a_job_whose_deadline_passed_before_it_ran`, `tests/unit/test_worker.py::test_worker_times_out_a_job_whose_deadline_already_passed`. **Partial:** a handler that starts before the deadline and overruns it is not interrupted — the per-stage watchdog is deferred, so "late completion discarded" is untested |
| F7 | Cancellation races (mid-run, mid-retry, after completion) | jobs | unit: cancel token; integration: cancel mid-build; assert documented race outcome | MVP | covered for the implemented semantics (ADR 0015): queued → `canceled` and running → `cancel_requested` at the next attempt boundary, plus 409 on a terminal job (`tests/integration/test_job_lifecycle.py`, `tests/unit/test_worker.py::test_worker_cancels_a_failing_job_when_cancellation_was_requested`, `tests/contract/test_ingest_api.py::test_cancel_endpoint_reports_missing_and_conflicting_jobs`). **Partial:** mid-call cancellation is a documented non-goal until stage handlers have checkpoints, so a long single call still runs to completion |
| F8 | Partial pipeline (ingest ok, index fails) | jobs chaining | integration: inject index failure → heal → child completes | Baseline | planned |
| F9 | Disk full / artifact write failure | storage | integration: injected write error (tmpdir quota/monkeypatch) → clean `failed`, no dangling row | Baseline | partial: `tests/unit/test_artifacts.py::test_artifact_store_detects_corrupt_existing_blob`; disk-full/write-failure recovery remains planned |
| F10 | Postgres down mid-request/job | catalog, api | integration: stop/start DB → 503 + retry/backoff, no state loss | Baseline | planned |
| F11 | Restart with in-flight jobs (NFR-006) | jobs | integration: restart mid-job → lease sweep requeues | MVP | planned |
| F12 | GPU OOM / GPU absent (NFR-007) | workloads, observability | unit: workload raising OOM; CI: no-GPU admission path (`gpu_required` rejected, telemetry degrades) | MVP | planned |
| F13 | Telemetry/log sink failure | observability | unit: broken sink during stage → pipeline unaffected | MVP | planned |
| F14 | Build nondeterminism (NFR-004) | builds | CI test: build → clean → rebuild → identical manifest hash + directory hashes | MVP | planned |
| F15 | Orphan blobs / stale staging dirs | storage | integration: kill build mid-materialize → `gc` removes orphans | Baseline | planned |
| F16 | Lineage inconsistency | catalog | unit: edge write + API visibility (`tests/unit/test_catalog_repository.py::test_register_episode_persists_artifact_and_lineage`, `tests/e2e/test_ingest_flow.py::test_api_worker_artifact_and_lineage_end_to_end`); rollback/audit deferred | Baseline | partial: write + read path covered; transactional rollback/audit remains planned |

Additional classes required by [../spec/00-original-specification.md](../spec/00-original-specification.md) §6 to be
disaggregated here as implementation reveals them: network failure (S3-tier trigger era), duplicate *data* (same
episode ingested from two sources — distinct from F4), invalid API pagination/cursor misuse.

Audit rule: a stage gate may not claim "failure-mode catalog covered by tests" (definition-of-done stage 3) while any
row is `planned`.
