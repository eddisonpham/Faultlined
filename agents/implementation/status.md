# Implementation Status

Updated whenever a component changes state. One row per component from `architecture/components.md`.
State: `not started` / `stub` / `working` / `hardened`.

| Component | State | Tests | Notes |
|---|---|---|---|
| 1. API service (`api/`) | not started | none | Planned phase 04 scaffold, working in phase 05 vertical slice |
| 2. Catalog (`catalog/`) | not started | none | Schema outline: `architecture/storage.md`; migrations from phase 05 |
| 3. Job queue + scheduler (`jobs/`) | not started | none | State machine: `architecture/data-flow.md` §4; ADR 0005 |
| 4. Worker pool (`jobs/worker.py`) | not started | none | Process pool; heartbeats/leases per `compute-orchestration.md` |
| 5. Ingest (`ingest/`) | not started | none | Readers: MCAP + LeRobot first; ROS 2 bag deferred (requirements §7) |
| 6. Validation (`validation/`) | not started | none | Rule registry + reason codes (failure-modes F2/F3) |
| 7. Indexing (`indexing/`) | not started | none | Parquet exports are rebuildable cache (`storage.md` §5) |
| 8. Builds (`builds/`) | not started | none | Determinism test (NFR-004) lands with the first build path |
| 9. Workload runner (`workloads/`) | not started | none | Narrow interface (`api.md`); reference workload = benchmark-style CPU job |
| 10. Artifact store (`storage/`) | not started | none | Content-addressed layout (`storage.md` §2) |
| 11. Observability (`observability/`) | not started | none | JSON logs + telemetry + metrics (ADR 0008); conventions already seeded |
| 12. CLI (`cli.py`) | not started | none | One command per NFR-011 task |
| 13. Engineering UI (`frontend/`) | not started | none | Scope frozen (`architecture/frontend.md`); design + framework phase 12 |
| 14. Benchmark harness (`benchmarks/`) | not started | none | Methodology + metric schema already seeded; first bench phase 06 |
