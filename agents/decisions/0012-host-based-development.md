# 0012. Host-based local development; containers are optional

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** implementer + architect agents (phase 04), consistent with owner-approved deployment constraints

## Context

The captured development machine has no Docker/Podman runtime, and PostgreSQL is an explicit local service prerequisite
(ADR 0005). Phase 04 asks for a clean-clone workflow and containerization "where appropriate"; requiring a container
runtime would contradict the local-development architecture and prevent the owner from running the project. The local
tier is defined in [../architecture/deployment.md](../architecture/deployment.md).

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Host Python processes + local PostgreSQL; containers optional (chosen)** | Runs on the confirmed machine; easy debugger/test loop; no container prerequisite | OS-level PostgreSQL install and service management; host config differs across machines |
| Docker/compose mandatory for dev | Reproducible multi-service bundle; easy CI parity | Docker absent on the confirmed machine; cannot meet clean-clone acceptance there; adds GPU/container runtime questions |
| Embedded DB to avoid local service | One-command setup | Conflicts with accepted ADR 0005 and its concurrency rationale; would require a superseding storage/job decision |

## Decision

The supported local-development and production-like-local tiers run **on host processes**. PostgreSQL is installed as a
local service; Python API/worker processes run via `just`/`uv`. Dockerfiles/compose are **not required and are deferred**;
they may be added as optional packaging only after a real deployment target and a local runtime are available. This is
not a change to the data architecture or storage decision.

## Consequences

- (+) All implemented commands are testable on the captured Windows machine without Docker.
- (+) Explicit runtime behavior makes the PostgreSQL service prerequisite visible rather than hiding it in a container.
- (−) A clean clone still requires PostgreSQL installation/configuration; the README documents it.
- (−) Cross-OS PostgreSQL service differences need runbook notes before Production Baseline.

## Docs updated

- [../architecture/deployment.md](../architecture/deployment.md)
- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §10
- [../implementation/coding-standards.md](../implementation/coding-standards.md)
- [../../README.md](../../README.md)
