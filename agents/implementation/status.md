# Implementation Status

Updated 2026-09-28 after phase 06 implementation and scaffolding review. Phase 05/06 work remains uncommitted in this checkout. The synthetic episode slice passes local unit/contract/E2E tests; PostgreSQL integration tests skip without operator-provided `DE_DATABASE_URL`. The review rejects stage acceptance pending benchmark baseline authorization/correctness, runtime observability follow-ups, live-DB and clean-clone verification. One row per component from `architecture/components.md`.
State: `not started` / `stub` / `working` / `hardened`. `working` means exercised for the documented synthetic path, not production-ready.

| Component | State | Tests | Notes |
|---|---|---|---|
| 1. API service (`api/`) | working | `tests/contract/test_ingest_api.py`, `tests/unit/test_api_endpoints.py`, `tests/e2e/test_ingest_flow.py` | POST ingest job; GET jobs/episodes with persisted lineage; OpenAPI contract |
| 2. Catalog (`catalog/`) | working | `tests/unit/test_catalog_repository.py`, `tests/unit/test_catalog_database.py`, `tests/integration/test_ingest_job.py` | Postgres jobs/artifacts/episodes/lineage schema; idempotent repository; inline idempotent schema init, formal migrations deferred |
| 3. Job queue + scheduler (`jobs/`) | working | `tests/unit/test_job_state.py`, `tests/unit/test_catalog_repository.py`, `tests/unit/test_job_queue.py` | Postgres enqueue/claim/terminal transitions; retries, leases, timeouts remain deferred |
| 4. Worker pool (`jobs/worker.py`) | working | `tests/unit/test_worker.py`, `tests/integration/test_ingest_job.py`, `tests/e2e/test_ingest_flow.py` | `de dev` starts one worker process; production process pool/heartbeats not implemented |
| 5. Ingest (`ingest/`) | working | `tests/unit/test_worker.py`, `tests/integration/test_ingest_job.py`, `tests/e2e/test_ingest_flow.py` | `SyntheticEpisodeIngestService` canonicalizes/stores synthetic JSON; MCAP/LeRobot readers not implemented |
| 6. Validation (`validation/`) | stub | `tests/unit/test_reason_codes.py` | Stable reason codes exist; rule engine phase 05 |
| 7. Indexing (`indexing/`) | not started | none | Parquet exports are rebuildable cache (`storage.md` §5) |
| 8. Builds (`builds/`) | not started | none | Determinism test (NFR-004) lands with the first build path |
| 9. Workload runner (`workloads/`) | not started | none | Narrow interface (`api.md`); reference workload = benchmark-style CPU job |
| 10. Artifact store (`storage/`) | working | `tests/unit/test_artifacts.py`, `tests/unit/test_worker.py` | Atomic filesystem SHA-256 blob store with dedupe/checksum verification |
| 11. Observability (`observability/`) | stub | `tests/unit/test_logging.py`, `tests/unit/test_metrics.py`, `tests/unit/test_telemetry.py` | JSON logging/correlation in API and worker; host/GPU snapshot and metric primitives exist. Runtime queue/job/stage metric call sites are absent; initial psutil nullable-field fallback is unverified. See review finding 4. |
| 12. CLI (`cli.py`) | working | `tests/unit/test_cli.py`, `tests/unit/test_cli_commands.py` | `doctor`, `api`, `worker`, `dev`, `gc`; `gc` is still a placeholder |

| 13. Engineering UI (`frontend/`) | not started | none | Scope frozen (`architecture/frontend.md`); design + framework phase 12 |
| 14. Benchmark harness (`benchmarks/`) | stub | `tests/unit/test_benchmark_harness.py`, `tests/unit/test_benchmark_statistics.py`, `tests/unit/test_benchmark_placeholder.py` | Schema/statistics/provenance and synthetic local harness implemented; no measured baseline or result record. Default `just bench` cannot pass without baseline; failed-run and hardware-compatibility checks have initial uncommitted fixes that need verification (review findings 1–3). |
