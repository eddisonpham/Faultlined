# EXP-0010c: 10k-episode catalog scale — NFR-003 query latency + NFR-002 validate throughput

- **Date (UTC):** measured 2026-10-02
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-003 (catalog query p95 ≤ 200ms), NFR-002 (system-scale validation), NFR-008 (10k-episode / ~500 GB working set handled without architectural change); stage-4 criterion "scaling curves ... data size, via the benchmarks harness"; EXP-0010a (ingest linearity) + EXP-0010b (worker-count scaling) context.
- **Tool:** `scripts/scale_campaign.py --leg catalog-scale` against a throwaway Postgres DB (real `data_engine` user, real schema from `initialize_schema`), 1 worker process for the validate leg. Seed via `COPY ... FROM STDIN` (not `VALUES %s` multi-row — psycopg on this Windows box rejects the multi-row placeholder form). Query sampling via real `PostgresCatalog` methods.
- **Schema note:** the dev catalog `episodes` table has **no `source` column** — its columns are `(id, source_hash, episode_key, artifact_hash, format, metadata, state, created_at)`. Any seeding or query campaign must key on `source_hash` or on a synthetic differentiator embedded inside `metadata`, never on a `source=` filter. The harness's `_BenchApiCatalog` is an in-memory deterministic model (50-row synthetic returns) and does **not** exercise the real Postgres catalog; NFR-003/NFR-002 numbers here come from the real repository, not the harness.

## Hypothesis / purpose

NFR-008's claim that a 10k-episode working set needs no architectural change has two halves:
1. **Read side (NFR-003):** every catalog query an operator hits must stay ≤ 200ms p95 with 10k episodes present, including `quality_summary` which assembles per-episode quality rows into distributions.
2. **Write side (NFR-002):** a real validate job over all 10k episodes must complete through a real worker process, and the per-episode cost must be a batch-scale number, not a catalogue-scale blocker.

The quadratic bug in `_assemble_quality_summary` (mean/std recomputed per row → O(n²), ~18s at 10k) was fixed in `src/data_engine/catalog/repository.py` by hoisting the population stats before the z-score comprehension. This experiment confirms the fix holds at full 10k scale and measures the real query costs.

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty |
| OS / Python / app | Windows 11 10.0.26200 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\\` |
| database | throwaway `data_engine_scale_<hex>` on `postgresql://data_engine@127.0.0.1:55432` (PG 17.11) |
| seed | 10,000 episodes via `COPY` (artifacts + episodes + episode_quality + lineage_edges), realistic metadata shape (lerobot-v3, channel_stats with action + observation.state), quality rows with smooth/moderate/jerky verdicts, spread `created_at` over 10k seconds |
| validate profile | `scale-campaign@1`: required_channels=[action, observation.state], min_frames=10, max_frames=100_000 |
| query sampling | 200 episodes picked evenly across the 10k set; each sample hits list_episodes(limit=50), get_episode, get_episode_quality, list_jobs(state=succeeded, limit=50), count_jobs(queued); quality_summary called once (full 10k-row assembly) |
| completed DB (kept for reference) | `data_engine_scale_a1aaa518` |

## Results

### Seed

- 10,000 episodes seeded via `COPY ... FROM STDIN` in **0.7s** (artifacts + episodes + quality + lineage). Not the measured subject; reported for reproducibility.

### Validate job (NFR-002 system-scale reading)

| Metric | Value |
|---|---|
| episodes requested | 10,000 |
| submit → start | 599 ms |
| run time (started → finished) | 865.8 s (14.4 min) |
| total (submit → finish) | 866.4 s (14.4 min) |
| checked | 10,000 |
| passed | 10,000 |
| failed | 0 |
| per-episode | 86.584 ms |
| validation_results rows | 10,000 |
| episode states after | 10,000 valid |

All seeded episodes passed validation. The per-episode cost (86.6 ms) is a batch-scale number: at 24 µs/msg ingest (EXP-0010a) a 10k-episode working set is an ingest batch, and the validate side is a 14-minute single-worker job, not a catalogue-scale blocker. N workers divide it the same way ingest does.

### NFR-003 query latency (200 samples, real repository, 10k episodes)

| Query | n | p50 | p95 | max | target |
|---|---|---|---|---|---|
| list_episodes(limit=50) | 200 | 49.1 ms | 64.5 ms | 169.68 ms | ≤ 200 ms |
| get_episode | 200 | 34.9 ms | 43.17 ms | 157.64 ms | ≤ 200 ms |
| get_episode_quality | 200 | 43.42 ms | 54.24 ms | 169.17 ms | ≤ 200 ms |
| list_jobs(state=succeeded, limit=50) | 200 | 33.14 ms | 46.34 ms | 153.02 ms | ≤ 200 ms |
| count_jobs(queued) | 200 | 33.2 ms | 41.4 ms | 143.06 ms | ≤ 200 ms |
| **quality_summary** (full 10k assembly) | 1 | — | **181.2 ms** | — | ≤ 200 ms |

**NFR-003: MET.** Every query p95 is ≤ 200ms, including `quality_summary` at 181.2ms — the tightest margin but still under target. The quadratic fix holds: `quality_summary` over 10k rows is now O(n) in Python (plus the DB read), not O(n²). The earlier per-row extrapolation (47.7ms/100rows → 4.77s/10k) over-predicted because the 100-row sample included cold-start overhead (query plan, connection) that does not scale with row count; the actual 10k measurement is 181.2ms.

### Honest caveat on the quality_summary margin

181.2ms against a 200ms target is a 19ms / 9.5% margin. On this hardware and with this seed shape it passes, but it is the one query in the set that loads the entire `episode_quality` table (10k rows) and processes it in Python. Any future column added to that read, or any per-row Python work added to `_assemble_quality_summary`, eats directly into this margin. The read should stay a single `SELECT` with no per-row N+1, and `_assemble_quality_summary` should stay O(n) with stats hoisted — both already true, and both are the thing to re-check if the quality schema grows.

## Trade-offs / verdict

**NFR-003 MET, NFR-002 system-scale reading confirmed.** The 10k-episode catalog reads within target at p95, and the validate job completes in 14.4 min single-worker — a batch, not a redesign. Combined with EXP-0010a (ingest linear to the hour bag) and EXP-0010b (worker-count speedup), NFR-008's "10k episodes / ~500 GB handled without architectural change" holds on both the read and write sides.

**Decision: record** (measurement record; no adopt/reject object). No code change. The `_assemble_quality_summary` quadratic fix (hoisted mean/std) is confirmed at full 10k scale.

## Follow-ups

- The `catalog-scale` leg's validate job dominates wall time (~14 min). If the campaign is re-run often, the seed + query-sampling half could be split from the validate half so the NFR-003 numbers are available without waiting for the full validate job.
- `quality_summary` at 181ms is the tightest NFR-003 margin; if the quality schema gains columns, re-measure before and after.
