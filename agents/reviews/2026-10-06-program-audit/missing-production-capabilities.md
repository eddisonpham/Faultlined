# Missing production capabilities — what this program cannot do yet

- **Date:** 2026-10-06 · **Tree:** `693edb3` · Companions: [architecture.md](architecture.md),
  [technical-debt.md](technical-debt.md), [proposed-v2.md](proposed-v2.md).
- "Production" here means: a robotics team running this on shared infrastructure, on real data, with
  more than one human. Each item is a capability gap that exists in the code today, not a wishlist entry;
  items that are merely *nice* for a local-first tool are marked as such.

## 1. Security and access control

| Gap | Evidence | Consequence |
|---|---|---|
| **No authentication of any kind** | no auth code in `src/data_engine/api/` (grep-verified); every route is anonymous | Safe only while bound to `127.0.0.1` (`config.py::api_host` default). Setting `DE_API_HOST=0.0.0.0` exposes every mutation — job submission with arbitrary filesystem paths (`ingest_source` `payload.source` is read by the worker), slice deletion (`DELETE /api/v1/slices/{id}`), vocabulary merge/split — to the network |
| **No authorization / tenancy** | single implicit actor; vocabulary events record no actor | Undo/merge decisions cannot be attributed; two teams sharing a catalog can undo each other's decisions with no audit identity |
| **No CSRF posture** | forms POST without tokens | Moot without cookies/auth today; becomes live the moment any auth is added to the UI |
| **Path ingestion is an arbitrary-file-read primitive** | `jobs/worker.py::_run_handler` `ingest_source` takes any absolute path the worker can read | By design for a local tool; a hard blocker before any multi-user deployment |
| **Secrets handling is good but narrow** | ADR 0002, `SecretStr`, `.env` never read by the app | Fine today; no vault/KMS integration exists for shared deployments |

## 2. Data lifecycle and durability

| Gap | Evidence | Consequence |
|---|---|---|
| **No garbage collection** | `cli.py` gc branch is a stub (T-13) | Orphaned blobs, `.pending-*` crash residue (EXP-0011), and superseded exports grow forever |
| **No retention/compaction policy** | metrics JSONL is append-only; `runtime.jsonl` unbounded | EXP-0007 tail-read mitigates reads (500k records ≈ 30 s full parse before), but nothing prunes |
| **Manual backup/restore only** | [docs/runbooks/backup-and-restore.md](../../../docs/runbooks/backup-and-restore.md) is procedural | No scheduled dumps, no restore drill automation, no point-in-time recovery story |
| **Single Postgres, no HA story** | `catalog/database.py::connect` is one DSN | Acceptable for local-first; catalog loss = full loss of curation history |
| **Exports are local-directory only** | `builds/export.py` writes `settings.export_root` | No object store (S3/GCS) publish path for team-shared datasets |

## 3. Pipeline and product surface

| Gap | Evidence | Consequence |
|---|---|---|
| **Validate / build / export unreachable from the browser** | `api/app.py::ui_submit_job` builds only `ingest`/`ingest_source` (EXP-0017) | The product's stated output (curated dataset builds) is API-only; a non-programmer operator cannot complete the core loop |
| **No slice creation from the browser** | slices UI is read-only (`/ui/slices` GET + detail); `POST /api/v1/slices` is JSON-only | Curation's downstream step needs curl |
| **No batch ingestion of a directory** | one `ingest_source` job per path; no scan/reconcile | "Ingest last night's captures" is N form submissions or a script |
| **No format adapters beyond LeRobot + MCAP-JSON** | `ingest/readers/registry.py` (EXP-0014: CDR/protobuf MCAP, ROS 2 bag, UMI HDF5, RLDS, zarr, WebDataset all refused or degenerate) | Day-one operator data is usually *not* LeRobot; the format boundary is the adoption boundary |
| **No webhook/notification egress** | ADR 0020's notifier is deterministic classification into the incidents table; no delivery channel (planned B2 needs ADR 0031) | Incidents are invisible unless someone opens `/ui/incidents` — which today shows nothing anyway (T-04) |
| **No build diff / dataset comparison** | planned B5, absent | "What changed between build A and B" is unanswerable — the core question for dataset versioning |

## 4. Operations and deployment

| Gap | Evidence | Consequence |
|---|---|---|
| **No hosted CI** | [.github](../../../.github) has only a PR template (T-11) | Gates run only when remembered |
| **No container/deployment artifact** | no Dockerfile/compose/deploy dir | Every deployment is a hand-built workstation; onboarding cost is high |
| **No horizontal API story** | `create_app` holds catalog in `app.state`; fine to scale reads, but uvicorn single-process is the documented mode | Acceptable until concurrent teams; then needs a documented multi-worker uvicorn + N workers layout (the queue already supports it — EXP-0010b) |
| **Windows-specific runner config** | justfile `windows-shell := ['C:\Program Files\Git\bin\bash.exe', ...]` | Linux/macOS contributors must bypass or edit the justfile |
| **No scheduling primitive** | nothing runs on an interval except worker reaping/host sampling (T-04) | The monitor, rollups (B3), and any nightly job have no home |

## 5. Observability gaps

| Gap | Evidence | Consequence |
|---|---|---|
| **Monitor never executes** | T-04 | The observability stack detects nothing in production |
| **No alert egress** | see webhooks above | Even fixed, a fired incident has nowhere to go |
| **No detection-accuracy figures** | B-016 unrun; no precision/recall claim exists | The monitoring design is unvalidated against injected faults end to end |
| **Job rows lose error structure** | T-03/T-14 | The metrics sink has reason codes but the operator surface has "job handler failed" |
| **No request tracing beyond correlation ids** | `observability/logging.py` | Fine at current scale; no span/trace export if this ever fronts a fleet |

## 6. Data-model / API gaps

| Gap | Evidence | Consequence |
|---|---|---|
| Aggregate endpoints return whole-table JSON | `quality/summary` 2.2 MiB (T-07) | Any dashboard consumer pays 400 ms + 2.2 MiB per refresh |
| Vocabulary unmapped queue pagination is offset-style (`after_episodes`/`after_task` cursor is paired-required) | `api/vocabulary_schemas.py` | Adequate; but no server-side filtering by task/robot on the queue |
| No export of the vocabulary/decision log as a first-class artifact | events endpoint is list-only | Team-shared curation history has no bulk extract |
| Baselines cover ingest workloads only | `benchmarks/baselines/` holds two (T-17, owner-gated) | validation, build/export, vocabulary and read surfaces cannot be regression-checked |

## 7. What is deliberately missing (not a gap)

- No model inference in any decision path (ADR 0020), no quantization/TensorRT/ONNX scope (CLAUDE.md
  scope guard) — correct omissions.
- No SPA framework, no build step (ADR 0014) — correct for a local-first tool; revisit only if the UI
  mutation surface multiplies (see [proposed-v2.md](proposed-v2.md) P1).
- No multi-tenant SaaS ambitions in the current spec — see the problem statement
  ([agents/spec/problem.md](../../spec/problem.md)).
