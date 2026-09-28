# 0010. Deployment shape: modular monolith with out-of-process workers

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 03), per owner-approved ADR 0003

## Context

The engine has an API surface, a job pipeline, and background stages (components in
[../architecture/components.md](../architecture/components.md)). The question is the process/service topology for the
local-first, no-Docker hardware ([../spec/environment.md](../spec/environment.md)) while keeping failure isolation for
long-running stage work (failure modes F5, F12 in [../architecture/failure-handling.md](../architecture/failure-handling.md)).

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Modular monolith + out-of-process workers (chosen)** | One deployable and one mental model; module boundaries enforced by import rules ([repo-layout.md](../architecture/repo-layout.md)); worker processes give real isolation (crash/OOM/GPU) where it matters; trivial local dev | Module boundaries are convention + CI checks, not hard walls |
| Single process (API runs stages inline) | Simplest possible | A slow/crashing stage takes down the API; no cancellation/timeout isolation; GIL |
| Microservices (API, scheduler, stage services) | Hard boundaries | Operational burden impossible without containers here; premature at one user/one machine |

## Decision

One Python package (`data_engine`, ADR 0004) deployed as **two process classes**: the API process (optionally
supervised workers in dev) and worker processes executing stage handlers via the handler registry
([api.md](../architecture/api.md) internal interfaces). Module boundaries and dependency direction are enforced by
repo-layout rules and an import check in CI. Workers are the unit of failure isolation: crash, OOM, GPU faults, and
cancellation all resolve at the process/job level (compute-orchestration.md).

## Consequences

- (+) Dev loop: `de dev` runs everything; tests exercise handlers in-process and workers out-of-process.
- (+) Failure semantics (at-least-once, leases, heartbeats) live in one codebase and one test suite.
- (−) If a future stage needs a different language/runtime (e.g. Rust reader), it must enter as a library (FFI,
  ADR 0004 trigger) or a separate long-running helper — needs a new ADR.
- (−) Vertical scaling only; multi-node requires superseding ADR 0005/0010 together.

## Docs updated

- [../architecture/compute-orchestration.md](../architecture/compute-orchestration.md), [../architecture/repo-layout.md](../architecture/repo-layout.md)
- [../architecture/trade-offs.md](../architecture/trade-offs.md) row 8
