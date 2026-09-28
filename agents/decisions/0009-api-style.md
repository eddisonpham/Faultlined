# 0009. API style: HTTP+JSON with FastAPI and OpenAPI contracts

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

FR-013 requires documented API contracts with pagination, error model, and idempotency; MVP criteria require API
contract tests; the engineering UI (FR-016) and workload clients are the consumers. All traffic is local or
LAN-at-most in the deployment tiers we support ([../spec/environment.md](../spec/environment.md)). Scored matrix:
[../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §2.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **FastAPI + Pydantic, HTTP+JSON, OpenAPI (chosen)** | Request/response schemas are the contract; auto OpenAPI for contract tests and UI client generation; async where it matters; ubiquitous in ML tooling | Async discipline needed to avoid blocking the loop |
| Flask + marshmallow | Simple | Manual schema/validation/OpenAPI; more glue code |
| gRPC / protobuf | Strong schemas; streaming | Toolchain friction (Windows codegen), poor browser story for the UI, no current streaming need (vetoed on necessity) |
| GraphQL | Flexible queries | A query layer over our own catalog API solves a problem we don't have |

## Decision

The platform exposes **HTTP+JSON** APIs implemented in **FastAPI**, with **Pydantic** models as the single source of
truth for schemas; OpenAPI served from the app and committed as a contract artifact for CI drift checks. Conventions:

- Resource model: `/jobs`, `/episodes`, `/datasets`, `/builds`, `/runs`, `/artifacts`, `/lineage`, `/health`, `/metrics`
  (names finalized in `api.md`, phase 03).
- Errors: RFC7807-style problem objects with stable machine-readable `code` (data contracts, requirements §4).
- Pagination: cursor-based on list endpoints.
- Idempotency: `Idempotency-Key` header on job submission (ties to ADR 0005 semantics).
- Versioning: URL prefix `/api/v1`; additive evolution preferred, breaking changes gated by version bump.
- Auth stance: **local single-user, no auth in v1** (bind to localhost); deferred trigger per requirements §7
  (second real user) — never invent a home-grown auth scheme.

Model/workload execution is accessed only through the narrow workload interface (FR-012): clients submit workloads as
jobs; no model code runs in the API process.

## Consequences

- (+) Contract tests can diff live OpenAPI against the committed artifact in CI.
- (+) UI clients generate cleanly from Pydantic/OpenAPI; curl-able for debugging.
- (−) Not ideal for high-frequency streaming ingest; the trigger in the matrix keeps gRPC on the table if it arises.
- (−) No auth means localhost binding is a security boundary — documented in `deployment.md` and the README.

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §2
- [../research/technology-matrix.md](../research/technology-matrix.md) (FastAPI core; gRPC excluded-for-now)
- [../spec/requirements.md](../spec/requirements.md) §4 (API contract)
