# Vertical Slice: Synthetic Episode Ingest Job

**Status: implemented in phase 05.** Chosen to prove the real API → Postgres queue → worker → content-addressed
artifact → episode/lineage catalog → API read path on the owner's local-first architecture. It uses a tiny synthetic JSON
episode to avoid network downloads and robotics dependencies; **it is not an MCAP or LeRobot parser**.

## End-to-end path

1. `POST /api/v1/jobs` submits an `ingest` job with a synthetic episode payload and optional `Idempotency-Key`.
2. FastAPI validates the request and the Postgres catalog atomically inserts the job (duplicate keys replay the same job).
3. A worker claims one queued job (`FOR UPDATE SKIP LOCKED`), canonicalizes the episode JSON, writes the bytes to the
   SHA-256 artifact store, and registers the episode + `produced_by` lineage edge + successful job state (separate DB transactions in this slice; crash recovery/atomic cross-store commit is deferred).
4. `GET /api/v1/jobs/{id}` returns terminal job state and output episode ID.
5. `GET /api/v1/episodes/{id}` returns episode metadata, source hash, artifact hash, and persisted `produced_by` lineage edges.
6. JSON logs carry a correlation ID; the response returns it.

## Demonstration

Prerequisites: Python 3.14 + `uv` + `just`; PostgreSQL running locally, a local `data_engine` database and operator-set
`DE_DATABASE_URL` in the shell or gitignored `.env` (README Quickstart). The agent must not open `.env`.

```bash
just setup
just run
# In another Git Bash terminal, submit through the public API:
curl -sS -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-episode-001' \
  -d '{"type":"ingest","payload":{"episode":{"task":"pick_place","robot":"synthetic_arm","timestamps":[0.0,0.1],"observations":[[0,0],[1,1]],"actions":[[0.1,0.1],[0.2,0.2]]}}}'
# Poll the returned job ID, then follow episode_id from its result:
curl -sS http://127.0.0.1:8000/api/v1/jobs/<job-id>
curl -sS http://127.0.0.1:8000/api/v1/episodes/<episode-id>
```

## Components proven

- API service + OpenAPI resource/error contract (FastAPI).
- PostgreSQL catalog and idempotent jobs table (real DB, not a fake).
- Worker lifecycle: claim → execute handler → terminal state; failure is visible and doesn't mark partial work complete.
- Filesystem artifact store: canonical bytes, SHA-256 path, checksum verification.
- Episode + artifact + lineage metadata queryable back through API.
- Correlation ID returned and included in structured logs.

## Tests

- Unit: canonical JSON/hash stability, artifact-store dedupe and checksum, state transition guard, payload validation, repository behavior, worker/service path.
- Integration (`DE_DATABASE_URL` required): real Postgres bootstrap, duplicate idempotency, claim→run→episode+lineage.
- End-to-end: TestClient POST public API → run worker → GET job + episode through public API using a test catalog adapter and the real artifact store (`tests/e2e/test_ingest_flow.py`). A separate Postgres-marked E2E check validates the live catalog schema when configured.
- Failure path: malformed episode rejected by API with 422 and no queued job; worker handler exception transitions job to `failed` (integration).
- Postgres integration tests skip when `DE_DATABASE_URL` is absent; this keeps default gates runnable on a clean machine, but does not substitute for running the live-DB demo against an operator-configured database.

## Explicitly out of scope

MCAP/LeRobot readers; rule-based quality validation/quarantine; Parquet indexing; dataset selection/build/export;
workload model execution; retries/leases/heartbeats/cancellation race semantics; frontend; full metric/telemetry system;
production migrations; real public data downloads. These remain architecture commitments and are staged into later MVP work.
