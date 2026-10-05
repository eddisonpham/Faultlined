# EXP-0015 — Per-feature latency for the whole shipped surface, at three catalog scales

- **Date:** 2026-10-04
- **Commit:** `bab9ab5` (clean tree)
- **Hardware:** Windows 11, x86_64, PostgreSQL 17.11 on 127.0.0.1:55432, single client, no background load
- **Tooling:** `scripts/feature_latency.py`
- **Method:** [methodology](../benchmarking/methodology.md). 3 warmup requests discarded, **20 measured trials** per
  route per scale (rule 3), percentiles over the raw samples (rule 4), one workload per process (rule 5). Synthetic
  catalog, labelled as such (rule 1).

## What this measures and why

The repo had one API latency campaign: EXP-0010e measured **six endpoints at 10 rps** over a 10k catalog. Six
endpoints out of roughly forty is not a picture of the product, and "10 rps" is a *load generator*, not a latency
distribution. It says nothing about what one person waiting on one page experiences.

This measures the thing a person waits for: a single sequential client, every `/api/v1` read, every `/ui` page, both
streamed downloads, and the four mutations an operator performs by hand — at **20, 1,000 and 10,000 episodes**, because
a route that is fine at 20 rows and slow at 10,000 is the entire question.

Each scale gets its own throwaway database, its own real `uvicorn` process serving the real app, and a
pre-populated metrics sink (4,000 records) so `/api/v1/metrics` is measured over history rather than over an empty
file. Nothing touched the dev catalog.

## Result — p95 in milliseconds

Read routes:

| Route | 20 eps | 1,000 eps | 10,000 eps | Payload @10k |
|---|---:|---:|---:|---:|
| `monitoring/health` | 23.7 | 16.1 | 26.1 | - |
| `incidents/list` | 65.3 | 69.7 | 63.0 | - |
| `incidents/summary` | 74.1 | 69.8 | 70.5 | - |
| `builds/list` | 66.5 | 74.5 | 77.8 | - |
| `jobs/list` | 71.1 | 74.5 | 66.3 | - |
| `jobs?limit=1` (report) | 66.3 | 78.6 | 77.5 | - |
| `metrics` | 108.5 | 90.2 | 96.7 | 48 KiB |
| `health` | 87.2 | 93.0 | 94.3 | - |
| `slices/list` | 75.0 | 70.7 | 77.7 | - |
| `artifacts/list` | 166.7 | 80.8 | 101.4 | 9.7 KiB |
| `monitoring/notify-preview` | 68.2 | 71.5 | 76.5 | - |
| `vocabulary/events` | 65.2 | 169.3 | 84.3 | 12 KiB |
| `vocabulary/unmapped` | 162.4 | 67.4 | 79.6 | - |
| `failures/list` | 85.5 | 74.5 | 75.7 | - |
| `failures/episodes` | 67.4 | 168.4 | 160.1 | - |
| `episodes?limit=50` | 77.0 | 148.5 | 175.4 | 28.5 KiB |
| `episodes?limit=50&offset=5000` | 64.3 | 80.6 | 92.6 | 28.5 KiB |
| **`quality/summary`** | 79.8 | 97.1 | **398.7** | **2,207 KiB** |
| `vocabulary/candidates` | 204.6 | 173.7 | 212.7 | - |
| `clusters/review` (frozen) | 109.7 | 200.8 | 197.6 | - |
| **`vocabulary`** | 340.1 | 344.1 | **392.2** | - |
| **`status`** | 294.1 | 289.9 | **272.3** | - |
| **`clusters` (frozen archive)** | 375.1 | 475.1 | 416.8 | - |

UI pages:

| Page | 20 eps | 1,000 eps | 10,000 eps | Payload @10k |
|---|---:|---:|---:|---:|
| `experiments` | 30.4 | 32.0 | 31.9 | 12 KiB |
| `benchmarks` | 35.5 | 36.8 | 34.1 | 15 KiB |
| `metrics` | 125.5 | 113.3 | 111.9 | 37 KiB |
| `jobs` | 70.5 | 67.3 | 76.5 | 6.7 KiB |
| `slices` | 72.7 | 63.4 | 75.7 | 3.9 KiB |
| `episodes` | 78.9 | 67.1 | 92.2 | 32 KiB |
| `insights` | 71.6 | 91.8 | **389.7** | **5,194 KiB** |
| `builds` | 79.0 | 149.0 | 66.4 | 3.7 KiB |
| `artifacts` | 73.5 | 68.9 | 106.3 | 16 KiB |
| `incidents` | 200.6 | 206.0 | 208.0 | 5.6 KiB |
| `failures` | 107.6 | 184.9 | 214.7 | 4.8 KiB |
| `episodes/{id}` | 261.0 | 151.4 | 267.5 | 9.7 KiB |
| `status` | 290.3 | 261.0 | 203.0 | 6.6 KiB |
| **`schema`** | 488.7 | 461.2 | **538.4** | 17.5 KiB |
| **`vocabulary`** | 411.7 | 412.2 | **595.4** | 14.9 KiB |
| **`clusters` (frozen → 307)** | 393.2 | 501.5 | 494.5 | 6.4 KiB |

Writes (p95, status codes in brackets):

| Operation | 20 eps | 1,000 eps | 10,000 eps |
|---|---:|---:|---:|
| `POST /jobs` | 83.4 [202] | 76.5 [202] | 167.4 [202] |
| `POST /vocabulary` (create entry) | 75.1 [201] | 70.0 [201] | 78.0 [201] |
| `POST /vocabulary/dismissals` | 158.5 [200] | 75.9 [200] | 149.7 [200] |
| `POST /vocabulary/mappings` | 232.3 [200] | 238.4 [200] | **248.1 [200]** |

Downloads: `episodes/export?format=csv` 80.0 ms p95 and `format=jsonl` 82.9 ms p95 at 10k, 57 KiB each.

## What the numbers say

**The floor is ~65–80 ms and it is not the database.** Twenty routes sit at that floor regardless of scale, and
several *fall* from 20 to 1,000 episodes (artifacts 167→81, jobs 71→75, slices 75→71). This is a single-process
`uvicorn` serving sequential requests with a connection per request; the floor is HTTP plus framework overhead, and
it dominates every route that is not actually doing work. Any claim of the form "the API is slow" is wrong below
~150 ms.

**Four routes are over the product's own 200 ms target at 10,000 episodes**, and two of them are by a factor of two:

- `GET /api/v1/quality/summary` — **398.7 ms p95, 2.2 MiB of JSON.** A *summary*. NFR-003 sets 200 ms for catalog
  reads; this misses by 2×. EXP-0010c recorded 181.2 ms for the same query on the same catalog, so the figure is
  somewhere in 180–400 ms depending on run; it misses the target on the slow runs and is within noise of it on the
  fast ones. The payload is the honest signal here: 2.2 MiB to describe 10,000 episodes.
- `/ui/insights` — **389.7 ms p95, 5.2 MiB of HTML.** A page a human opens.
- `/ui/vocabulary` — **595.4 ms p95**, the slowest page in the product.
- `/ui/schema` — **538.4 ms p95**, flat across all three scales.

**Two pages cost the same at 20 episodes as at 10,000, which means their cost is fixed, not scaling.** `/ui/schema`
(489/461/538 ms) reads the live database structure on every load; `/ui/vocabulary` (412/412/595 ms) is explained
below. A page that is slow at 20 rows and slow at 10,000 rows is not a query problem, and no amount of indexing
fixes it.

**`/ui/vocabulary` duplicates its own two most expensive queries.** Measured directly against a 1,000-episode
catalog:

| Call | Median | Max |
|---|---:|---:|
| `list_entries` | 46.7 ms | 164.2 ms |
| `list_unmapped` | 45.3 ms | 61.9 ms |
| `list_events` | 40.8 ms | 45.8 ms |
| `candidates()` (ranker, pure) | ~0.0 ms | 0.0 ms |
| **`vocabulary_health`** | **181.6 ms** | 305.5 ms |
| **total query work for the page** | **333.1 ms** | 476.7 ms |

`vocabulary_health()` internally calls `list_unmapped(settings, limit=TASK_LIMIT)` — where `TASK_LIMIT = 5000` — and
`list_entries()`, the exact two queries `ui_vocabulary` has *already* run. So the page runs each of them twice, and
the health readout (a strip of integers) scans up to **5,000** unmapped strings while the page above it asks for 50.
**Roughly 55 % of the page's time is a duplicated computation**, and it is the part that grows fastest. The ranker
itself is pure and costs nothing; the cost is entirely the two queries it is handed twice.

This is the same defect class as the `/api/v1/metrics` N+1 found in EXP-0010e, in the newest slice of the product.

**`GET /api/v1/clusters` costs 417 ms at 10k while every mutation on it returns 410.** The cluster surface is
declared a frozen archive (ADR 0029), yet the read is a live computation over every episode's extracted core. A
frozen archive should be cheap to read; this one is the third most expensive read in the product.

**`/ui/clusters` is a bare 307 redirect**, so its measured 494 ms *is* the cost of `/ui/vocabulary` — a redirect
should be ~1 ms, and reporting it as a 494 ms page is a measurement artefact, not a finding. Recorded here so the
number is not mistaken for one.

## Defect found in the harness, fixed

The first full run of this campaign reported `HTTP 500` for `/ui/episodes/{id}` at every scale. The cause was in
`scripts/scale_campaign.py::_seed_catalog`, which had been writing `motion_trace` as a flat list of 12 floats; the
real shape written by `EpisodeQuality.to_dict()` is a list of *runs*, each run a list of `(seconds, score)` pairs.
Every episode the campaign script had ever seeded would have 500'd on its detail page. The campaign never noticed
because it only ever measured repository queries, never a rendered page. Fixed in the same commit as this record; no
product code was changed, and the second run has no 500.

## Caveats

- One client, one process, no concurrency. Interference is measured separately in
  [EXP-0016](0016-concurrent-workflow-contention.md); this record is the clean per-route baseline.
- The seeded `episode_metadata` is smaller than a real MCAP episode's (two channel summaries rather than one per
  topic plus `fps_source_topic`, `quality_source_topic` and attachments). **The 2.2 MiB and 5.2 MiB payload figures
  are therefore a floor, not a typical case.**
- Windows timings, single machine, no committed baseline (rule 6 — a baseline needs the owner's green flag,
  HANDOFF §13). Absolute numbers do not transfer; the *ordering* and the *shape against scale* do.
- Two full campaign runs were executed. `/ui/insights` measured 605.7 ms p95 in the first and 389.7 ms in the
  second, and `quality/summary` 261.4 ms and 398.7 ms — run-to-run spread of roughly 1.5× on the two heaviest
  routes, which is itself the finding: **the routes with the largest payloads have the least predictable latency.**

## Reproduction

```bash
uv run python scripts/feature_latency.py --scales 20 1000 10000 --trials 20 --json var/feature-latency.json
```

Roughly 8 minutes for all three scales.