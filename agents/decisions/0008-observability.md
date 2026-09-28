# 0008. Observability: structured JSON logs, NVML telemetry, file-based metrics

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

FR-014/FR-015 and the scaffolding definition-of-done require structured logs, correlation IDs across components, metric
naming conventions, and resource telemetry that degrades without GPU. The observability conventions
([../observability/conventions.md](../observability/conventions.md)) and benchmark metric schema
([../benchmarking/metric-schema.md](../benchmarking/metric-schema.md)) already prescribe the formats. The system is one
API process + local workers on a laptop — there is no scrape fleet, and no traces to store yet.
Scored matrix: [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §8.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Stdlib `logging` JSON formatter + pynvml + metrics files (chosen)** | Zero services; correlation IDs trivially propagated via context; GPU telemetry works on the dev box and degrades cleanly; metrics land where benchmarks read them | No query UI (we build minimal views); manual span-ish timing fields |
| structlog / loguru now | Nicer ergonomics | Adds a dependency for what a ~50-line formatter does (optional later) |
| OpenTelemetry + Prometheus + Grafana now | Industry-standard; great resume keyword | Nothing to scrape/trace at this scale; 3 extra services; pure accumulation (vetoed on necessity) |
| Tempo / Jaeger | Trace backends | Backends without a tracer (vetoed) |

## Decision

- **Logs:** stdlib `logging` emitting one JSON object per line, with required fields per
  [../observability/conventions.md](../observability/conventions.md) (timestamp, level, logger, event, correlation IDs:
  `ingest_id`/`job_id`/`run_id`/`episode_id`, component, message + structured extras). Correlation IDs flow through job
  payloads and HTTP middleware (trace-context style `X-Correlation-Id`).
- **Resource telemetry:** `pynvml` for GPU utilization/VRAM (feature-detected — absent GPU logs `gpu_present=false` and
  continues), stdlib for CPU/RAM/disk via `psutil`-free platform calls where practical (dependency decision recorded in
  [../research/technology-matrix.md](../research/technology-matrix.md) if `psutil` is added).
- **Metrics:** named per [../benchmarking/metric-schema.md](../benchmarking/metric-schema.md) conventions; runtime
  counters/timers persisted as metric rows (same schema as benchmarks) so runtime and benchmark data compare in one place.

**Deferred with triggers:** OTel spans when ≥ 3 concurrently communicating components exist; Prometheus exposition when a
scraper exists; Grafana when an ops dashboard need outgrows the engineering UI.

## Consequences

- (+) Observability works on day one with zero services and is fully testable in CI (including the no-GPU path).
- (+) One metric schema for runtime and benchmarks — comparisons across experiments stay trivial (spec §8).
- (−) No distributed tracing story until the trigger; ad-hoc timing fields cover current needs.
- (−) If the system grows services, migration to OTel is real work (accepted; trigger-gated).

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §8
- [../research/technology-matrix.md](../research/technology-matrix.md) (OTel/Prometheus/Grafana optional-deferred; structlog optional)
