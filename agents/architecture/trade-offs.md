# Trade-offs

Top architectural trade-offs, each with the ADR that records it. "We chose X over Y because Z; we accept W."

| # | Trade-off | Chose | Over | Because | We accept | Record |
|---|---|---|---|---|---|---|
| 1 | Build vs adopt (scheduling) | Thin Postgres queue + workers | Ray / K8s+Argo / Temporal / Celery | The job lifecycle is our deliverable (spec §4); single machine; must be failure-testable directly | We own retry/cancel/lease semantics and their tests | [0005](../decisions/0005-catalog-and-job-queue.md) |
| 2 | Build vs adopt (lineage/experiments) | Manifests + run records in our catalog | MLflow / W&B / DVC | Provenance must be atomic with data; episode-level lineage exceeds tracker models | No free experiment UI; our UI must cover it | [0007](../decisions/0007-lineage-and-run-records.md) |
| 3 | Formats | Adopt MCAP + LeRobot v3 + Parquet | Bespoke format | Ecosystem interop + demo data; physical-data storage lessons (#23) | Format quirks become our problem; conversions sometimes needed | [0006](../decisions/0006-storage-and-formats.md) |
| 4 | Storage tier | Local FS content-addressed | S3/MinIO, Delta/Iceberg | 228 GB NVMe suffices; no multi-machine need | Single-disk ceiling (~NFR-008 reality); migration must stay possible | [0006](../decisions/0006-storage-and-formats.md) |
| 5 | Language | Python (+ process workers) | Rust / C++ | Ecosystem + iteration speed; no measured hot path | GIL → process-level parallelism; possible future FFI cost behind a measured trigger | [0004](../decisions/0004-language-and-toolchain.md) |
| 6 | API | HTTP+JSON FastAPI | gRPC / GraphQL | Contract tests, browser-friendly UI, ubiquitous tooling | Not optimal for high-frequency streaming ingest | [0009](../decisions/0009-api-style.md) |
| 7 | Observability | JSON logs + NVML + file metrics | OTel/Prometheus/Grafana | Nothing to scrape/trace at this scale; metric schema shared with benchmarks | Manual timing fields; migration work if services appear | [0008](../decisions/0008-observability.md) |
| 8 | Deployment shape | Modular monolith + out-of-process workers | Microservices | One deployable, simple dev; workers give isolation where it matters | Module boundaries are convention + import checks, not service walls | [0010](../decisions/0010-modular-monolith-and-workers.md) |
| 9 | Delivery semantics | At-least-once + idempotent handlers | Exactly-once claims | Exactly-once is unachievable across process death honestly; content addressing makes repeats harmless | Duplicate executions must be observable (metrics) and tested | [0005](../decisions/0005-catalog-and-job-queue.md) |
| 10 | Metadata store | PostgreSQL | SQLite (embedded simplicity) | Concurrent workers + API + transactions; industry-credible | One local service to install/operate (documented in deployment.md) | [0005](../decisions/0005-catalog-and-job-queue.md) |
| 11 | Determinism cost | Canonical manifests + sorted materialization | Faster nondeterministic builds | Rebuild identity is the product promise (NFR-004) | Slight build overhead; discipline required in every hashing path | [0006](../decisions/0006-storage-and-formats.md) + [0007](../decisions/0007-lineage-and-run-records.md) |
| 12 | Scope | Data engine only; eval platform deferred | A+B combined story | One coherent problem; hardware cannot host credible sim-based eval | B's users must wait; the dataset contract is designed for them | [0003](../decisions/0003-problem-selection.md) |

Revisit discipline (CLAUDE.md): trade-offs change only with evidence — an experiment record or failure drives an ADR
that supersedes the row above; never a silent swap.
