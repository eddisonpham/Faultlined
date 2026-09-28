# 0007. Lineage, versioning, and experiment tracking: build-thin records in the catalog

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

FR-006–FR-008 and FR-011 require dataset versioning, bidirectional lineage, and reproducible run records (spec §9);
NFR-004 makes rebuild determinism a hard gate. MLflow (#27) is the canonical off-the-shelf tracker; OSMO tracks lineage
source→model natively (#14). We already own a transactional catalog (ADR 0005) and content-addressed artifacts
(ADR 0006) — the lineage question is whether to store provenance ourselves or adopt a tracker's schema now.
Scored matrix: [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §6, §7.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Build-thin: manifests + lineage edges + run records in our catalog (chosen)** | Exactly the schema our requirements name; one store, atomic with jobs; no server/UI dependency; deterministic hashing stays under our control | We build (small) record schemas and query APIs |
| Adopt MLflow now | Rich UI, model registry, many integrations | Its run/lineage model doesn't cover episode-level provenance we need anyway; extra service + Python 3.14 compatibility risk; two sources of truth |
| Weights & Biases | Polished UI | Hosted SaaS; reproducibility/local-first conflict (vetoed on necessity) |
| DVC / lakeFS | Versioning off the shelf | Wrong model for binary episode corpora (see ADR 0006) |

## Decision

**Build thin.** The catalog stores: dataset **build manifests** (canonical JSON: content hash, sorted source episode
hashes, validation profile hash/version, query, config, code commit + dirty flag, env capture, timestamps), **lineage
edges** (source episode → build; build → run), and **run records** (spec §9 fields: commit, config, dataset hash,
model identity, hardware/software capture, seeds, metrics). Model versioning is minimal: named workload artifacts with
content hashes — a registry UI is out of scope.

**Deferred with trigger:** MLflow adoption is deferred until run-comparison/registry needs outgrow catalog SQL +
UI (definition-of-done stage ≥ Production Baseline evaluation); if adopted, it becomes a *projection* of our records,
never the source of truth.

## Consequences

- (+) Provenance lives atomically with the data it describes; no dual bookkeeping.
- (+) Reproducibility test (NFR-004) exercises our own code end-to-end.
- (−) No free experiment UI — our engineering UI must render lineage/runs (FR-016) acceptably.
- (−) Schema evolution of manifests is our responsibility (version the manifest schema from day one).

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §6, §7
- [../research/technology-matrix.md](../research/technology-matrix.md) (MLflow/W&B optional-deferred)
- [../spec/requirements.md](../spec/requirements.md) §5 (reproducibility)
