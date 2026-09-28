# Observability Conventions

Seed conventions; phase 06 fills in concrete libraries and field names.

## Principles
- Every metric or log field must inform a decision. Delete ones that don't.
- Structured (JSON) logs to stdout. Human-readable rendering is a dev-only formatter.
- A **correlation ID** flows API → queue → worker → storage → logs → metrics → UI. Job ID and episode ID are first-class fields.
- Never log secrets, tokens, or full file contents.

## Required log fields
`timestamp` (UTC, ISO 8601), `level`, `service`, `event` (stable snake_case name), `message`, `correlation_id`,
`job_id` (when applicable), `episode_id` (when applicable), `error.type` / `error.message` / `error.stack` (on errors).

## Metrics
- Naming: `<domain>_<object>_<unit>` snake_case with base units (`_seconds`, `_bytes`, `_total`).
- Labels are low-cardinality only (never job ID or episode ID as a label).
- Levels: system (host/GPU), platform (queue depth, job states, worker health), pipeline (stage durations), episode (validation results, sizes), error/failure.

## Resource telemetry
- GPU via NVML when available; degrade gracefully (no GPU ≠ error). CPU/RAM/disk/network via the OS.
- Collected at fixed cadence and attached to benchmark runs.

## Tracing
- Decide in an ADR (phase 03/06) whether distributed tracing is warranted or correlation IDs + structured logs suffice.

## Documented here as the source of truth
TODO(phase 06): metric registry table (name, type, unit, labels, decision it informs).
