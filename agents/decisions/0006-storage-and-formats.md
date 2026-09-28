# 0006. Storage and data formats: local content-addressed artifacts, MCAP + LeRobot v3 + Parquet

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

The engine's contract with the world is format-shaped (FR-001, FR-006, FR-018; data contracts in
[../spec/requirements.md](../spec/requirements.md) §4). Industry is standardizing on MCAP for raw multimodal logs
(#20, #36 — default in ROS 2) and LeRobot v3 (Parquet + MP4 + metadata) for curated robot-learning datasets (#34, #35);
OXE pooled 60 datasets into one schema (#26). Rerun's analysis (#23) shows physical data needs chunked/columnar storage
with rich metadata, not row-per-timestamp tables. Disk budget ~228 GB local NVMe; single machine; no Docker
([../spec/environment.md](../spec/environment.md)). Scored matrix: [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §5.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Local FS, content-addressed artifacts; MCAP in, LeRobot v3 out, Parquet/Arrow metadata (chosen)** | Ecosystem interop and free demo data; crash-safe append-only raw logs; deterministic content hashes (FR-007); zero services | Single-disk scale ceiling; no multi-machine sharing |
| S3/MinIO from day one | Cloud-native interchange (OSMO pattern #31) | Extra service + credentials; no current volume/multi-machine need; deferred with trigger |
| Delta Lake / Iceberg | ACID lakehouse versioning | Spark/Trino-scale engines; catastrophic overkill at our scale (vetoed) |
| DVC / lakeFS for versioning | Off-the-shelf versioning | Models fit source trees/object stores, not GB binary episodes (see ADR 0007) |
| Bespoke episode format | Total control | Permanent translation tax; loses Foxglove/LeRobot/Rerun tooling (vetoed) |

## Decision

- **Ingest boundary:** MCAP containers and LeRobot v2/v3 directories are the accepted raw forms (ROS 2 bag adapter
  deferred per requirements §7).
- **Curated output:** LeRobot v3 dataset directories, byte-deterministic layout (canonical ordering, stable hashing).
- **Metadata:** indexed into PostgreSQL (OLTP, FR-004) **and** exported as Parquet tables for analytical scans
  (DuckDB-compatible, optional add-on deferred until queries outgrow catalog SQL).
- **Artifacts** (raw copies, builds, workload outputs): local filesystem with **content addressing** —
  `sha256` keyed store layout `artifacts/<algo>/<prefix>/<hash>`, immutable, deduplicated; the catalog records all
  logical names → hashes. Determinism rules from requirements §5 apply to everything hashed.
- **Lineage:** manifests reference source hashes, never mutable names alone (FR-006–FR-008).

**Deferred with triggers:** S3/MinIO tier (volume or multi-machine); Delta/Iceberg (concurrent writers on object
storage); bespoke formats (never without an ADR superseding this one).

## Consequences

- (+) Interops with Foxglove/Rerun/LeRobot tooling; public datasets usable as fixtures and demos.
- (+) Content addressing makes rebuild determinism (NFR-004) and at-least-once idempotency (ADR 0005) natural.
- (−) We implement the manifest/canonicalization logic ourselves (thin, well-tested — FR-007 test in CI).
- (−) Local disk is the ceiling; the storage.md layout must keep an object-store migration path open.

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §5
- [../research/technology-matrix.md](../research/technology-matrix.md) (MCAP/LeRobot/Parquet core; S3/Delta/lakeFS/DVC classes)
- [../spec/requirements.md](../spec/requirements.md) §4 (data contracts)
