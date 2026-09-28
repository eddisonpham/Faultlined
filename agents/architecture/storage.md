# Storage

Decisions: ADR [0005](../decisions/0005-catalog-and-job-queue.md) (Postgres catalog),
[0006](../decisions/0006-storage-and-formats.md) (content-addressed artifacts, MCAP/LeRobot v3/Parquet),
[0007](../decisions/0007-lineage-and-run-records.md) (manifests + lineage). Data contracts:
[../spec/requirements.md](../spec/requirements.md) §4.

## 1. Metadata schema outline (PostgreSQL)

Schema evolves via numbered migrations (`src/data_engine/catalog/migrations/`). Outline, not DDL:

| Table | Key columns | Purpose |
|---|---|---|
| `episodes` | id, source_hash (unique), format, state, artifact_hashes (jsonb), created_at | One row per ingested episode; state: `ingested/valid/quarantined` |
| `episode_metadata` | episode_id, extractor_version, task, robot, t_start, t_end, duration_s, channel_stats (jsonb), quality_flags (jsonb) | Queryable index (FR-004/FR-005); upsert key (episode_id, extractor_version) |
| `validation_profiles` | hash (pk), name, version, rules (jsonb) | Immutable, hash-addressed profiles |
| `validation_results` | id, episode_id, profile_hash, passed, reason_codes (jsonb), created_at | Immutable per (episode_id, profile_hash) |
| `dataset_builds` | id, manifest_hash (unique), state, selection (jsonb), config (jsonb), created_at | Content-addressed builds |
| `build_sources` | build_id, episode_id, source_hash | Sorted source membership (lineage `contains`) |
| `lineage_edges` | id, from_type, from_ref, to_type, to_ref, relation | `derived_from`, `produced_by`, `used_by` (plus `contains` in build_sources) |
| `runs` | id, workload, workload_version, dataset_build_hash, config, env (jsonb), seeds (jsonb), state, metrics (jsonb) | Spec §9 provenance (FR-011) |
| `jobs` | id, type, state, priority, attempts, max_attempts, idempotency_key (nullable unique per type), payload (jsonb), parent_job_id, worker_id, lease_expires_at, deadline, created/started/finished | Lifecycle (ADR 0005) |
| `job_transitions` | job_id, from_state, to_state, at, reason | Audit trail for the state machine |
| `artifacts` | hash (pk), kind, size, created_at, refcount | Content-addressed inventory |
| `idempotency_keys` | key, request_hash, job_id, response (jsonb) | Idempotent submission (ADR 0009) |
| `metric_points` | id, name, value, unit, labels (jsonb), ts, source | Runtime + benchmark metrics (ADR 0008) |

Indexes: `episodes(source_hash)`, `episode_metadata(task, robot, t_start)`, `jobs(state, priority, created_at)`,
`lineage_edges(from_type, from_ref)` and `(to_type, to_ref)`, `metric_points(name, ts)`.

## 2. Artifact store layout (filesystem)

```text
$ARTIFACT_ROOT/                          # default: ./var/artifacts (gitignored)
  blobs/sha256/ab/cd/<full-hash>         # raw episode bytes, manifests, workload outputs (immutable)
  builds/<manifest_hash>/                # published LeRobot v3 directories (immutable once published)
    meta/  data/  videos/  ...           # LeRobot v3 layout, canonical ordering
  exports/parquet/<extractor_version>/   # episode metadata Parquet chunks (rebuildable cache)
  tmp/<job_id>/                          # staging; cleaned on completion or GC job
```

- **Content addressing:** every blob is `sha256` of its bytes; builds are addressed by `manifest_hash`
  (sha256 over canonical JSON of the manifest). Logical names live only in the catalog (FR-018).
- **Atomicity:** writes go to `tmp/<job_id>/` then `os.replace` into place; a catalog row is committed only after its
  artifacts exist (recovery job reconciles orphans/missing).
- **Versioning:** immutable artifacts + immutable manifests = versioning by construction; "dataset version" names in the
  UI are labels over hashes, never identity (FR-006).

## 3. Manifest and canonicalization (determinism)

Manifest v1 fields (canonical JSON — sorted keys, sorted source list, no wall-clock inside hashed content):

```json
{
  "schema_version": 1,
  "content_hash": "<sha256 of this manifest minus content_hash>",
  "sources": [{"episode_id": "...", "source_hash": "..."}],
  "selection": {"query": "...", "snapshot_at": "..."},
  "validation_profile": {"name": "...", "version": "...", "hash": "..."},
  "build_config": {"format": "lerobot_v3", "options": {}},
  "provenance": {"git_commit": "...", "git_dirty": false, "env": {"python": "3.14.x", "lockfile_hash": "...", "os": "..."}}
}
```

Timestamps (`snapshot_at`, `created_at`) are recorded **outside** the hashed content. Rebuild determinism (NFR-004) is
enforced by a CI test: build → clean → rebuild → compare `manifest_hash` and directory byte hashes.

## 4. Lineage model

| Relation | From → To | Meaning |
|---|---|---|
| `contains` | dataset_build → episode | Build membership (materialized in `build_sources`) |
| `derived_from` | dataset_build → episode/profile | Build derived from sources + profile |
| `produced_by` | artifact/run → run | Workload output provenance |
| `used_by` | dataset_build → run | Which build a run consumed |

Queries (FR-008): backward closure from a run to raw source hashes (answer "what data trained this?"), forward closure
from an episode to builds/runs ("where did this episode go?"). Depth-capped with cycle guard.

## 5. Caching strategy

- Immutable artifacts + immutable manifests mean **no invalidation** for data; caches are pure derivatives.
- Parquet metadata exports (`exports/parquet/`) are a **rebuildable cache** keyed by `extractor_version`; delete to
  refresh (GC/dedup job owns cleanup).
- Catalog is the hot path; Postgres shared buffers sized for the laptop (deployment.md).
- API read caches: none in v1 (measured first — "measure before optimizing"); cursor pagination keeps queries bounded.
- OS page cache does the heavy lifting for sequential MCAP/MP4 reads; readers are streaming (constant memory).

## 6. Storage budget

~228 GB free NVMe at capture ([../spec/environment.md](../spec/environment.md)). Accounting: `artifacts` refcounts +
per-job written-bytes metrics feed a `doctor`/GC story (orphan blobs, stale tmp dirs). Working-set target NFR-008
(10k episodes / 500 GB) exceeds free disk → documented as raw-side budget reality: benchmarks use scaled fixtures,
full-scale runs require external storage (S3 trigger in the decision matrix).
