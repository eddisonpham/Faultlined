# Failure Handling

Every row maps failure mode → detection → recovery → test. The test-facing catalog is
[../testing/failure-modes.md](../testing/failure-modes.md) (owned by test-engineer; kept in sync with this table).
Stance: fail closed on data, fail open on telemetry, never silently drop (overview principles 5–6).

| # | Failure mode | Component | Detection | Recovery | Test (planned) |
|---|---|---|---|---|---|
| F1 | Corrupt/truncated MCAP or malformed LeRobot dir | ingest | Reader parse failure / sniff mismatch | Reject with `INGEST_*` reason code; no catalog row; job `failed` (terminal — retrying won't fix data) | unit: reader fixtures; integration: submit corrupt file |
| F2 | Missing required channel / bad timestamps | validation | Rule violations (FR-002) | Episode `quarantined` + reason codes; no indexing; revalidate endpoint for remediation (FR-003) | unit: each rule; integration: quarantine flow |
| F3 | Invalid validation profile / config | validation, api | Schema validation at submission | 4xx `VALIDATION_PROFILE_INVALID`; job never created | contract: bad profile payloads |
| F4 | Duplicate job submission | jobs | `idempotency_keys` unique constraint | Replay original response (202 + original job); 409 on key reuse with different payload | unit + contract: double POST |
| F5 | Worker crash / kill mid-job | jobs, worker | Heartbeat lease expiry (`WORKER_LOST`) | Job requeued, attempts+1; at-least-once repeat absorbed by idempotent handlers | integration: kill -9 worker between claim and completion |
| F6 | Timeout: slow ingest/validate/build/workload | jobs | Worker watchdog + dispatcher sweep | `timed_out` state; late completion discarded; artifacts of the attempt GC'd | integration: handler sleeping past deadline |
| F7 | Cancel during retry / during run | jobs | `cancel_requested` + cooperative token | Graceful stop → `canceled`; force-kill after grace; race resolved per state machine | unit: cancel token; integration: cancel mid-build |
| F8 | Partial pipeline (ingest ok, index failed) | jobs chaining | Parent/child job states | Stages retry independently; child re-enqueued from parent transition; UI shows partial state | integration: inject index failure then heal |
| F9 | Disk full / artifact write failure | storage, worker | Write error, free-space check | Job `failed` (`IO_DISK_FULL`); no partial catalog row; GC frees orphans; `doctor` warns | integration: fill artifact volume (loopback/quotas) or inject write error |
| F10 | Catalog (Postgres) down / connection loss | catalog, api | Connection errors | API 503 retry-after; workers retry claim with backoff; jobs survive in DB (no in-memory-only state) | integration: stop/start Postgres mid-job |
| F11 | Restart with in-flight jobs | jobs, worker | Startup reconciliation sweep | `running` rows with expired leases → `queued` (NFR-006) | integration: restart worker/API mid-job |
| F12 | GPU OOM / GPU absent | workloads | CUDA OOM error; NVML absence | Run record `failed` (`GPU_OOM`, retriable once at lower batch if workload declares it); `gpu_required` rejected at admission without GPU; telemetry `gpu_present=false` | unit: fake workload raising OOM; CI runs the no-GPU path |

## Retryable vs terminal

F1 and F3 are not just "failures" — they are failures that **retrying cannot improve**, because the
input bytes are the input bytes. The worker encodes that as `_TERMINAL_FAILURES` in
`jobs/worker.py`: `ReaderError`, `InvalidJobPayload`, and `UnsupportedJobType` skip the retry branch
in `_settle_failure` and go straight to `failed`, spending exactly one attempt. Everything else
(connection reset, disk full, a lost artifact) keeps its retry budget, because those may well not
repeat.

Without this split, a bad episode burns `max_attempts` identical failures before the operator sees
`failed`, and `jobs_retries_total` counts noise. The classification lives with the exception types
rather than in a lookup table so a new reader failure is terminal by default, which is the safe
direction: a wasted retry is cheap, a retried-forever job is not.
| F13 | Telemetry/log sink failure | observability | Handler exceptions | Degrade to stderr; never block pipeline | unit: broken log sink during stage run |
| F14 | Build nondeterminism (hash mismatch on rebuild) | builds | CI determinism test | Release gate: failing NFR-004 test blocks merge; diff manifest to locate nondeterministic field | test: rebuild-twice comparison (CI) |
| F15 | Orphaned blobs / stale tmp dirs | storage | Refcount audit / age sweep | `gc` job removes unreferenced blobs and stale staging dirs | integration: kill build mid-materialize, run GC |
| F16 | Lineage inconsistency (edge without row) | catalog | Transactional writes + FK constraints | Prevention-first; audit query in `doctor` | unit: transaction rollback leaves no dangling edge |

Reason-code registry (`INGEST_*`, `VALIDATION_*`, `IO_*`, `GPU_*`, `WORKER_LOST`, …) is defined in
`src/data_engine/observability/reason_codes.py` at implementation time and treated as API-stable (ADR 0009 rule:
add, don't rename).
