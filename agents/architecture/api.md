# API and Interfaces

Style decisions: ADR [0009](../decisions/0009-api-style.md) (FastAPI, HTTP+JSON, OpenAPI). Contract artifact:
OpenAPI JSON committed at `docs/api/openapi.json` and drift-checked in CI (MVP criterion). All endpoints are under
`/api/v1` unless noted.

**No interactive contract viewer.** `/docs` and `/redoc` return 404: the browser surface is the operator UI at
`/ui`, and a generated Swagger page does not belong in an operator's navigation (ADR 0021). `/openapi.json`
remains, because the drift check is a machine consumer. Read the committed `docs/api/openapi.json` directly, or
run `uv run python scripts/openapi_contract.py --check`.

**Error representation splits by caller, not by content** (ADR 0021). A request to a `/ui/...` path gets a
full HTML error page carrying the status, the detail and the `correlation_id`, rendered in the operator's
selected theme with a retry control. A request to `/api/v1/...` gets the unchanged JSON problem object. Same
status code, same correlation id, different representation — a mistyped bookmark should not be a wall of raw
JSON with no way back.

## Resource model

| Resource | Key fields | Notes |
|---|---|---|
| `job` | id, type, state, priority, attempts, idempotency_key, parent_job_id, payload, created/started/finished | Types: `ingest`, `validate`, `index`, `build`, `workload`, `gc` |
| `episode` | id, source_hash, format (mcap/lerobot), state (ingested/valid/quarantined), metadata, artifact hashes | State transitions from validation |
| `validation_result` | id, episode_id, profile_hash, passed, reason_codes[] | Immutable per (episode, profile_hash) |
| `validation_profile` | name, version, hash, rules (YAML) | Versioned config artifact |
| `dataset_build` | id, manifest_hash, state (planned/building/published/failed), selection, config | Content-addressed by manifest hash |
| `run` | id, workload (type+version), dataset_build_hash, config, env capture, seeds, metrics, state | Spec §9 fields |
| `artifact` | hash (sha256), kind, size, created_at | Content-addressed (ADR 0006) |
| `lineage_edge` | from (type+id/hash), to, relation | Relations: `contains`, `derived_from`, `produced_by`, `used_by` |
| `metric_point` | name, value, unit, labels, ts, source (runtime/benchmark) | Shared schema (ADR 0008) |

## Endpoints (v1)

**Phase-05 implemented subset:** `POST /api/v1/jobs` (both `ingest` and `ingest_source`),
`GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `POST /api/v1/jobs/{id}/cancel`,
`GET /api/v1/episodes/{id}`, and `GET /api/v1/health`. Remaining
endpoints in the table are architecture targets, not implemented yet.

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/v1/jobs` | Submit job | `Idempotency-Key` header; returns 202 + job. Optional `max_attempts` (1-10) and `deadline_seconds` set the retry budget and deadline (ADR 0015); both are part of the idempotency request, so replaying a key with a different budget is a 409. Two request shapes share the endpoint — `ingest` carries the episode in the body, `ingest_source` names a dataset path and lets a reader interpret it |
| `GET /api/v1/jobs` | List jobs | Cursor pagination; filters: type, state, since |
| `GET /api/v1/jobs/{id}` | Job detail | Includes attempts + transitions |
| `POST /api/v1/jobs/{id}/cancel` | Cancel | Cooperative semantics (data-flow.md §4). Queued → `canceled`; running → `cancel_requested`. 404 unknown, 409 on a terminal job |
| `POST /api/v1/episodes/ingest` | Convenience: submit ingest for source | Creates an `ingest_source` job; the direct `POST /api/v1/jobs` with `type: ingest_source` is the same thing and is what exists |
| `GET /api/v1/episodes` | Search episodes | Metadata predicate query (FR-005); cursor pagination. Phase-06 subset implemented: `state`/`flag`/`limit` filters |
| `GET /api/v1/episodes/{id}` | Episode detail | Includes metadata + lineage edges (phase 05 slice); validation results join in later MVP work |
| `POST /api/v1/episodes/{id}/revalidate` | Re-validate with profile | FR-003 (no re-ingest) |
| `GET /api/v1/episodes/{id}/lineage` | Forward lineage | Builds/runs containing this episode |
| `GET/POST /api/v1/validation-profiles` | Read / register profiles | Profile hash-addressed |
| `POST /api/v1/builds` | Plan + submit build job | Returns manifest draft hash |
| `GET /api/v1/builds` | List builds | Filters: manifest hash, state |
| `GET /api/v1/builds/{id}` | Build detail | |
| `GET /api/v1/builds/{id}/manifest` | Build manifest | The reproducibility record |
| `GET /api/v1/builds/{id}/lineage` | Backward lineage | Sources + profile + commit |
| `POST /api/v1/runs` | Submit workload run | Workload interface only (FR-012) |
| `GET /api/v1/runs`, `GET /api/v1/runs/{id}` | Run records | Spec §9 fields |
| `GET /api/v1/artifacts/{hash}` | Artifact metadata / download | Checksum-verified reads |
| `GET /api/v1/metrics` | Query metric points | Shared runtime/benchmark schema |
| `GET /api/v1/health` | Liveness + dependency status | DB, disk, worker heartbeat summary |
| `GET /api/v1/openapi.json` | Contract | Mirrors committed artifact |

## Conventions

- **Pagination:** cursor-based: `?cursor=<opaque>&limit=<n>`; response `{"items": [...], "next_cursor": ...}`;
  stable ordering by (created_at, id).
- **Errors:** problem object —
  `{"type": "about:blank", "title": "...", "status": 4xx, "code": "STABLE_CODE", "detail": "...", "correlation_id": "..."}`
  with stable machine-readable `code` values (e.g. `VALIDATION_PROFILE_INVALID`, `IDEMPOTENCY_KEY_CONFLICT`,
  `JOB_NOT_CANCELABLE`, `BUILD_SELECTION_EMPTY`). Codes are part of the contract; add, don't rename.
- **Idempotency:** `Idempotency-Key` on `POST /jobs` and job-creating endpoints; same key + same payload → original
  response replayed; same key + different payload → 409 `IDEMPOTENCY_KEY_CONFLICT`.
- **Versioning:** `/api/v1` prefix; additive changes in place; breaking changes require `/api/v2` + ADR.
- **Auth stance (v1):** none; server binds `127.0.0.1` by default. Remote exposure is opt-in config and documented as
  insecure-by-design (single-user local tier). Multi-user auth is deferred (requirements §7) — never home-grown.
- **Correlation:** clients may send `X-Correlation-Id`; otherwise one is generated and returned in the header.
- **Downloads:** every list route accepts `?format=csv|jsonl` and answers with a streamed file
  (`Content-Disposition: attachment`); the CSV header is the response model's field order and rows
  stream from the catalog cursor (ADR 0030). The UI's Download links reuse the view's query string.

## Internal interfaces (narrow seams)

**Stage handler protocol** (worker-side; implementer-owned):

```python
class StageHandler(Protocol):
    name: JobType  # "ingest" | "validate" | "index" | "build" | "workload" | "gc"

    def run(self, ctx: JobContext, payload: Mapping[str, Any]) -> StageResult: ...

    # ctx: job_id, correlation_id, cancel_token, deadline, logger, metrics, catalog, artifact_store
    # StageResult: ok(bool), outputs(artifact refs + catalog deltas), reason_code(str | None), metrics
```

**Workload interface** (models are workloads — FR-012; benchmark-engineer + implementer co-own):

```python
class Workload(Protocol):
    name: str
    version: str
    resource: Literal["cpu", "gpu_optional", "gpu_required"]

    def run(
        self, dataset: DatasetView, config: Mapping[str, Any], ctx: WorkloadContext
    ) -> WorkloadResult: ...

    # DatasetView: read-only handle over a published dataset build (hash-verified)
    # WorkloadResult: artifacts, metrics (metric-schema rows), run metadata (seeds, env), success/reason
```

Rules: workload code imports nothing from `catalog/`, `jobs/`, or `api/` — only the `workloads/` SDK surface; the
runner owns all provenance capture so a workload cannot forget reproducibility fields. `gpu_required` workloads are
rejected at admission when no GPU is present (NFR-007), `gpu_optional` declare their fallback behavior in their
contract.

**Reader protocol** (ingest): `sniff(path) -> bool`, `read(path) -> EpisodeSource` per format; adding a format
(ROS 2 bag later) means adding one reader + tests — no core changes.

## Read endpoints added for the MVP UI (stage 2)

Added for the Status / Jobs / Artifacts pages; all are read-only, cursor-paginated, and bounded at
`limit<=200`. Implemented in `src/data_engine/api/app.py` with queries in
`src/data_engine/catalog/repository.py`; see [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/jobs` | Newest-first job page. Filters: `state`, `type`. Cursor: `before` (ISO timestamp). Returns `items` + `next_before`. |
| GET | `/api/v1/artifacts` | Newest-first artifact page with `episode_ids` per row. Cursor: `before`. |
| GET | `/api/v1/status` | Health, `queue_depth` by job state, artifact/episode counts, and host telemetry. |
| GET | `/api/v1/metrics` | Aggregated runtime telemetry from the JSONL sink (ADR 0017): per-metric+label summaries with p50/p95/p99, bucketed mean series for sparklines, live job-state counts, and derived worker heartbeat age. Optional `window_seconds`, `bucket_seconds`. |
| GET | `/api/v1/episodes` | Episode catalog with the same curation views as the UI: filters `state` (ingested/valid/quarantined), `flag` (jerky/stalled/short/long), `limit` (1–500). Rows carry the quality columns for ranking. |
| GET | `/api/v1/episodes/export` | Curated manifest for curation and dataset builds: the same filters plus content identity (source/artifact hashes) per row, with `generated_at` + echoed `filters` for provenance. Registered before `/api/v1/episodes/{episode_id}` so `export` is never read as an id. |
| GET | `/api/v1/failures` | Read-only aggregate of what is failing: reason-code counts, failures per profile and per format, `quarantined_count`, `episodes_evaluated`. An empty database returns zeroed counts rather than 404. |
| GET | `/api/v1/failures/episodes` | Quarantined episodes with the profile, reason codes, and violations that put them there. Filters: `reason_code` (must be a known code, else 422), `limit` (1–500), cursor `before`. |
| POST | `/api/v1/slices` | Save a named curation filter (`name`, `notes`, `filter_config` = `{state, flag}`). 409 `SLICE_NAME_CONFLICT` when the name is taken. Membership is recomputed on read, so the slice never goes stale. |
| GET | `/api/v1/slices` | Newest-first slice page with `member_count`; bounded and cursor-paginated. |
| GET | `/api/v1/slices/{id}` | One slice's metadata, filter config, and current member count. 404 for unknown ids. |
| PATCH | `/api/v1/slices/{id}` | Update name, notes, or filter config. Membership recomputes on the next manifest read. |
| DELETE | `/api/v1/slices/{id}` | Drop a slice (204). Episodes themselves are untouched — a slice is a view, not an ownership record. |
| GET | `/api/v1/slices/{id}/manifest` | The curated manifest for one slice: the export shape plus `slice_id`/`name`, with content identity per row. A filter that no longer parses yields 404 rather than silently widening to "everything". |
| GET | `/api/v1/episodes/{episode_id}/validation` | Validation verdicts per profile (ADR 0016): passed flag, reason codes, and full violations — why an episode passed or was quarantined. 404 for unknown episodes. |
| GET | `/api/v1/jobs/{job_id}/episodes` | Episodes a job produced, following the `produced_by` lineage edge (run inspection). 404 for unknown jobs. |
| GET | `/api/v1/jobs/{job_id}/report` | Run triage card: job summary + rollup of the episodes it produced (state/verdict/flag counts, length summary, mean movement/jerk/stall) + validation verdict counts with reason-code frequencies over those episodes. 404 for unknown jobs. |
| GET | `/api/v1/episodes/{episode_id}/quality` | Motion-quality signals computed at ingest (ADR 0018): movement score, normalized jerk, stall ratio, per-dim activity, verdict, and a read-time length z-score. 404 for episodes ingested before quality existed. |
| GET | `/api/v1/quality/summary` | Dataset-level curation view: episode-length histogram, speed distribution, cross-episode per-dim σ matrix, and top jerk / stall / length outliers with episode links. |

## Monitoring notifier ([ADR 0020](../decisions/0020-deterministic-monitoring-notifier.md))

Deterministic. No trained model, no LLM call, and no network dependency in the detection path;
every incident is a rule or a control limit over a feature vector and cites its own evidence.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/incidents` | The incident queue, newest first, cursor on `last_seen`. Filters: `severity` and `label` (both validated against the registry, else 422), `status`, `limit` (1–500), `before`. Read-only. |
| GET | `/api/v1/incidents/summary` | Queue shape for a header: counts by status, severity, and label, plus how many open incidents would interrupt the owner. |
| GET | `/api/v1/incidents/{id}` | One incident with its full evidence bundle. 404 for unknown ids. |
| POST | `/api/v1/incidents/{id}/ack` | Acknowledge. An append-only fact rather than a resolution, so a same-fault recurrence still dedups onto the same incident. 404 for unknown or already-resolved ids. |
| POST | `/api/v1/incidents/{id}/resolve` | Resolve. Terminal: a same-fault recurrence afterwards opens a new incident, subject to the cooldown. |
| GET | `/api/v1/monitoring/health` | The monitor's own state: last tick, schema version, baseline scope/warm/held counts, alert budget, the notify-label policy, and the registered detectors. A monitor that has silently stopped looks exactly like one with nothing to report, so this is a first-class endpoint rather than a log line. |
| POST | `/api/v1/monitoring/tick` | Evaluate one window on demand. The loop is a scheduler's job, not a webhook's; this exists so the notifier can be driven deterministically from a test, a benchmark, or an operator who wants to see what it would say now. |
| GET | `/api/v1/monitoring/notify-preview` | Plain text: exactly what a notifier would deliver right now. **Rendering only — nothing is sent.** Outbound email is deliberately not implemented and requires explicit owner authorization ([ADR 0020 §10.3](../decisions/0020-deterministic-monitoring-notifier.md)). |
| PUT | `/api/v1/contracts/{job_id}` | Declare a completion contract for a run: `expected_episodes`, `expected_valid_fraction`, `max_duration_seconds`, `deadline_at`. Idempotent per job; re-declaring resets the outcome to `pending`, because a revised expectation has not been evaluated yet. 404 for unknown jobs, 422 for an impossible value, and `extra="forbid"` so a typo'd field fails loudly. |
| GET | `/api/v1/contracts` | Declared contracts with their outcome and observed counts; filters `outcome`, `limit`, cursor `before`. |
| GET | `/api/v1/contracts/{job_id}` | One contract. 404 for unknown jobs or contracts. |

The UI pages themselves live under `/ui` and are not part of the versioned API surface. They poll the
same read model by re-requesting their own page with `X-Fragment: 1`, which returns only the polling body
so Python remains the single renderer.
