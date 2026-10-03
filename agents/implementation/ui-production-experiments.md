# Production Baseline Workflow Experiments

**Initial smoke recorded in [EXP-0008](../experiments/0008-production-baseline-workflow-smoke.md); scale campaign not run.** Executed alongside [phase 14](../prompts/14-production-baseline.md). Stage 3 remains open; this plan is not productionization.

## Scenario definition

A simulated robotics data engineer performs the real user path: select a real source → submit through the UI/API → wait for worker completion → inspect episode/task and quality → review an uncertain cluster candidate → save/confirm curation → build/export → verify manifest/hash/lineage. Every stage records a pass/fail predicate and the user-visible reason when absent. Follow-up scenarios cover platform operator queue/incident inspection and eval engineer export/reproducibility.

### Corpus and scale tiers

| Tier | Grounded data | Catalog target | What is labelled real |
|---|---|---:|---|
| Small | Existing local Hub fixtures/data for LeRobot v2 and v3 plus the committed MCAP fixture | 25–100 episodes (or every real episode available) | Only source files actually read and their verified SHA-256 |
| Medium | Full available real LeRobot task/episode metadata corpus; real MCAP in a separate path | 1,000–5,000 episode records | Source records only; any duplicated catalog rows or derived metadata are identified |
| Large | Real-source workload plus explicitly separate scale-expansion rows to 10k+ records with representative metadata cardinality | ≥10,000 records where safe | Generated expansion is explicitly **synthetic catalog pressure**, never called real data; no fabricated artifact bytes are counted as real ingest |

The current on-disk test fixtures may be a 203-frame slice and several files are not available locally; inspect `var/real-data/` first. Do not claim episode-scale coverage based only on task strings or one tabular Parquet shard. If a tier cannot be executed with actual available data and owner authorization, report it as not executed and preserve the scale-up as backlog.

## Controlled run protocol

- Capture clean git SHA/dirty flag, lock hash, Python/OS/hardware, Postgres version, source revision/hash/size, user scenario version/seed, DB reset id, API/worker settings, and background load.
- Reset into a dedicated test DB/artifact root per run only after explicit approval; do not call broad `just reset` on operator data. Verify row/artifact baseline before and after. All sample jobs use unique run-key prefixes and exact-ID cleanup.
- Three warmups where reasonable; at least five clean end-to-end trials at each tier; keep raw per-trial timings. If full runs exceed budget, use fewer trials as exploratory and mark them non-publishable.
- Run clean and fault-injected campaigns separately. Inject one failure at a time; stop if cleanup or isolation is uncertain.
- Report step latency distributions; API p50/p95/p99; queue wait/run; throughput; failures/retries/cancellations; SQL latency/rows; HTML/JSON response size; rendered height/layout overflow; API/worker RSS and CPU; disk bytes; review queue counts and action rates; output hashes and lineage checks.
- Do not publish a baseline or claim production NFR compliance without owner review.

## Local diagnostic meta-log

Path: `var/experiments/ui-workflows.jsonl` (gitignored). The runner overwrites it per campaign and caps it at 2 MiB / 10,000 records; if the bound is reached it stops recording and emits a summary rather than dropping measurements silently. Keep result times in benchmark raw output; this logger is for low-cardinality diagnosis only.

Allowed fields: `run_id` (random non-secret identifier), `scenario`, `tier`, `step`, `elapsed_ms`, `status`, `item_count`, `byte_count`, `rss_bytes`, `cpu_percent` (nullable), `operation` (bounded vocabulary), `error_type` (class name), and a digest of generated record IDs if correlation is required. Never store DSNs, credentials, dataset paths, raw task strings, record payloads, model data, email addresses, or user names. Keep this outside the repository; no files in `var/` are committed.

## Initial questions / stopping conditions

- Does direct LeRobot/MCAP ingest land the same task strings the review page groups?
- Does singleton/boundary triage have useful precision for real text, or is it merely a visible queue?
- Which whole workflow step becomes the first latency or memory wall as catalog size grows?
- Do users confuse automatic proposals and human-created classes?
- Can a build be repeated and export verified after a restart without manual database repair?

Database destructive resets, disk pressure, process kill, or any action that could affect non-test/operator data requires a separate safety review and approval. The first smoke used unique rows in the persistent `_test` catalog and did not reset it; the browser audit was read-only. Small/medium/large campaign runs remain unexecuted, and build/export was blocked by an unrelated legacy valid row with incomplete metadata.
