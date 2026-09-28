# 0013. Use psutil for cross-platform host resource telemetry

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** observability-engineer + benchmark-engineer agents (phase 06)

## Context

FR-015 and the observability specification require CPU/RAM/disk/network telemetry on every supported host, including
Windows, with GPU telemetry optional via NVML. The existing ADR 0008 committed to "stdlib for CPU/RAM/disk via
psutil-free platform calls where practical" but left implementation unresolved. Python's standard library lacks a
consistent cross-platform API for process memory, CPU utilization, disk usage, and network counters; direct Windows
CIM subprocesses or OS-specific code would be harder to test and maintain. The telemetry collector must degrade without
a GPU and is sampled by both the application and benchmark harness.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **psutil (chosen)** | Mature, cross-platform Python API; easy to mock; no service; Windows/Linux/macOS support | Native wheels / one extra runtime dependency |
| Platform-specific stdlib + PowerShell/WMI on Windows | No dependency | Inconsistent metrics, subprocess overhead, OS forks, fragile parsing, hard to test |
| `resource` module only | Stdlib on Unix | Not available on Windows; incomplete disk/network/device telemetry |
| OpenTelemetry host metrics | Standardized | Adds collector/SDK/exporter complexity explicitly deferred in ADR 0008 |

## Decision

Add **psutil** as a direct core dependency for CPU %, process RSS, system RAM, disk usage, and network I/O counters.
Sampling errors are represented as unavailable fields, not fatal pipeline errors. GPU metrics continue using optional
`nvidia-ml-py` (`pynvml`) and degrade to `gpu_present=false` when the driver/device is unavailable.

## Consequences

- (+) Same telemetry schema on the captured Windows laptop and CI/Linux runners.
- (+) Sampling and fallback paths are unit-testable without relying on machine state.
- (−) Adds a native-wheel dependency; phase-04 toolchain and phase-06 lockfile validate CPython 3.14 compatibility.
- (−) Some counters (network delta, CPU first-sample percentage) are snapshots/interval-dependent; benchmark docs state this.

## Docs updated

- [../observability/conventions.md](../observability/conventions.md) (collector fields)
- [../research/technology-matrix.md](../research/technology-matrix.md) (psutil core with rationale)
- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) (observability choice)
- `pyproject.toml` and `uv.lock`
