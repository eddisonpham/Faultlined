# Decisions (ADRs)

Canonical record of architectural decisions. Copy [0000-template.md](0000-template.md) to `NNNN-short-slug.md`
(next free 4-digit number). Write the ADR **before** implementing the change.

Statuses: `proposed` → `accepted` → `superseded by NNNN` (or `rejected`). Never rewrite an accepted ADR's decision;
supersede it with a new ADR and update the affected docs.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0002](0002-secrets-handling.md) | Secrets handling | accepted |
| [0003](0003-problem-selection.md) | Problem selection: robot episode data engine | accepted |
| [0004](0004-language-and-toolchain.md) | Language and toolchain: Python-first, C++/Rust deferred | accepted |
| [0005](0005-catalog-and-job-queue.md) | PostgreSQL catalog + thin job queue + worker pool | accepted |
| [0006](0006-storage-and-formats.md) | Content-addressed artifacts; MCAP + LeRobot v3 + Parquet | accepted |
| [0007](0007-lineage-and-run-records.md) | Build-thin lineage and run records (MLflow deferred) | accepted |
| [0008](0008-observability.md) | JSON logs + NVML telemetry + file metrics (OTel deferred) | accepted |
| [0009](0009-api-style.md) | HTTP+JSON FastAPI API with OpenAPI contracts | accepted |
| [0010](0010-modular-monolith-and-workers.md) | Modular monolith + out-of-process workers | accepted |
| [0011](0011-coverage-gate.md) | Initial coverage gate and ratchet policy | accepted |
| [0012](0012-host-based-development.md) | Host-based local development; containers are optional | accepted |
| [0013](0013-cross-platform-resource-telemetry.md) | Use psutil for cross-platform resource telemetry | accepted |
| [0014](0014-minimal-ui-server-rendered.md) | Minimal server-rendered UI, no bundler | accepted |
| [0015](0015-cooperative-job-lifecycle.md) | Cooperative job lifecycle: retries, deadlines, cancel | accepted |
| [0016](0016-json-validation-profiles.md) | JSON validation profiles | accepted |
| [0017](0017-runtime-metrics-aggregation.md) | Runtime metrics aggregation | accepted |
| [0018](0018-episode-quality-signals.md) | Episode quality signals are summarized at ingest | accepted |
| [0019](0019-benchmark-workload-isolation.md) | Benchmark workloads run in isolated processes | accepted |
| [0020](0020-deterministic-monitoring-notifier.md) | The monitoring notifier is deterministic | accepted |
| [0021](0021-frontend-instrument-pass.md) | Instrument UI pass: one client runtime, visible failure, no Swagger surface | accepted |
| [0022](0022-mcap-ingest-reader.md) | MCAP ingest: one file is one episode, and the file's own words are the only metadata | accepted |
| [0023](0023-quality-metrics-honesty.md) | Quality metrics: refuse what cannot be measured, judge by majority, and score time | accepted |
