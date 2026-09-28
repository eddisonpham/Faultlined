# Implementation Status

Updated after phase 04 (commit evidence: see git history). Phase-04 scaffold/tooling is implemented; phase-05 vertical-slice logic is pending. One row per component from `architecture/components.md`.
State: `not started` / `stub` / `working` / `hardened`.

| Component | State | Tests | Notes |
|---|---|---|---|
| 1. API service (`api/`) | stub | `tests/unit/test_api_app.py` | FastAPI factory only; endpoints phase 05 |
| 2. Catalog (`catalog/`) | not started | none | Schema outline: `architecture/storage.md`; migrations from phase 05 |
| 3. Job queue + scheduler (`jobs/`) | stub | `tests/unit/test_job_state.py` | State types/transition contract; Postgres queue phase 05 |
| 4. Worker pool (`jobs/worker.py`) | not started | none | Process pool; heartbeats/leases per `compute-orchestration.md` |
| 5. Ingest (`ingest/`) | not started | none | Readers: MCAP + LeRobot first; ROS 2 bag deferred (requirements §7) |
| 6. Validation (`validation/`) | stub | `tests/unit/test_reason_codes.py` | Stable reason codes exist; rule engine phase 05 |
| 7. Indexing (`indexing/`) | not started | none | Parquet exports are rebuildable cache (`storage.md` §5) |
| 8. Builds (`builds/`) | not started | none | Determinism test (NFR-004) lands with the first build path |
| 9. Workload runner (`workloads/`) | not started | none | Narrow interface (`api.md`); reference workload = benchmark-style CPU job |
| 10. Artifact store (`storage/`) | not started | none | Content-addressed layout (`storage.md` §2) |
| 11. Observability (`observability/`) | stub | `tests/unit/test_reason_codes.py` | Stable reason-code registry exists; log context/telemetry phase 06 |
| 12. CLI (`cli.py`) | stub | `tests/unit/test_cli.py` | Entry point registered; commands phase 05 |
| 13. Engineering UI (`frontend/`) | not started | none | Scope frozen (`architecture/frontend.md`); design + framework phase 12 |
| 14. Benchmark harness (`benchmarks/`) | stub | `tests/unit/test_benchmark_placeholder.py` | Placeholder; real harness phase 06 |
