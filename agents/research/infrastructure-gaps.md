# Infrastructure gaps: what operators still cannot do, and how to extend elegantly

**Date:** 2026-10-04 · **Status:** research complete, prioritized proposals below, ready to proceed
**Question.** Beyond clustering, what major inconveniences do companies / data workers still hit
that this platform does not solve — and can the engine extend into them *elegantly*, honoring the
existing constraints (local-first, dependency-free core, JSONL sink, no cloud — ADR 0012,
ADR 0014)?

Every external claim cites a URL + access date (research rules in [README.md](README.md)).
The three concrete complaints to answer: **we can't check historic data nicely, we can't download
anything, and we can't alert people.**

---

## 1. What the engine can and cannot do today (ground truth)

| Capability | Current state | Evidence in repo |
|---|---|---|
| Metric history | JSONL sink (`var/metrics/runtime.jsonl`), read back tail-first up to 200k records; `?window_seconds=` filter; p50/p95/p99 summaries + bucketed means | `observability/aggregate.py` (`read_metric_records`, `_read_tail`, `window_records`); `agents/observability/conventions.md` "Read-back (ADR 0017)" |
| History depth | Unbounded file growth, no rotation/retention, **no rollups** — a month of runtime data is either one huge file or a truncated tail; `GET /api/v1/metrics` cannot answer "what was queue depth last Tuesday" reliably | `aggregate.py` docstring: "not the history. That is load-bearing" |
| Download/export | One endpoint: `GET /api/v1/episodes/export` → JSON manifest (curation view + content hashes). No CSV, no Parquet, no streaming download, no `Content-Disposition` anywhere | `app.py` line 1364; `EpisodeExportResponse` in `api/schemas.py` |
| Alerting | Monitor detects → incidents table → `notify_preview()` renders text **and sends nothing** ("The delivery path is deliberately not implemented (ADR 0020 §10.3)") | `monitoring/service.py` `notify_preview` |
| Historical catalog views | Builds are content-addressed and reproducible (good), but there is no as-of query, no diff between two builds/manifests in the UI, no trend of quality/queue stats over time | `web/lineage.py`, build manifests |
| Analytics presentation | Server-rendered pages + sparklines (`web/charts.py`), benchmarks page reads `benchmarks/` off disk, experiments page indexes records; all single-snapshot, no time range picker, no export | `web/records.py`, `web/pages.py` |

---

## 2. What the industry says is still painful

### 2.1 Robot-data operations are the recognized frontier

- "Robotics is entering its operations era" — data infrastructure named Foxglove's single largest
  investment priority for 2026
  ([Jump Capital](https://jumpcap.com/insights/robotics-is-entering-its-operations-era/), accessed
  2026-10-04).
- Foxglove launched a unified **data search + curation** platform (Apr 2026) — find, organize,
  operationalize robotics data ([BusinessWire](https://www.businesswire.com/news/home/20260421818840/en/Foxglove-Launches-Unified-Data-Search-and-Curation-Platform-to-Accelerate-Physical-AI-Development),
  accessed 2026-10-04; product page: search at petabyte scale, curate results as sessions/events
  and "the training datasets that feed your next model iteration"
  ([Foxglove blog](https://foxglove.dev/blog/data-search-curation), accessed 2026-10-04)). The
  commercial battleground is exactly the operator surface we already have a slice of — and
  curation-to-training-dataset is their value loop, which our builds/exports already express.
- The "robot data gap": whoever builds infrastructure to collect, standardize, and manage data at
  scale holds the key ([SCSP ISF Voices 2026](https://scsp222.substack.com/p/isf-voices-2026-the-robotics-data),
  accessed 2026-10-04); corroboration ([IBM, Nov 2025](https://www.ibm.com/think/news/the-data-gap-holding-back-robotics),
  accessed 2026-10-04).
- Dataset audits find **no per-episode quality metrics, no validation gates, no QA process** in any
  of 10 major datasets, and "no way to delete bad episodes" in LeRobot-format data
  ([Traceplane audit, 2026](https://traceplane.ai/blog/we-audited-10-robotics-datasets), accessed
  2026-10-04). Our validation + quarantine + slicing already covers part of this; the audit's
  other findings — metadata lies, silent schema changes, missing episodes — are *history and
  provenance* problems, i.e. exactly the gaps in §1.

### 2.2 History: the standard answer is retention tiers + rollups, not bigger tails

- Prometheus's local storage is explicitly not long-term storage (15–30 day default retention);
  the ecosystem answer is tiered retention and downsampling/rollups for long ranges
  ([VictoriaMetrics single-server docs](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/),
  accessed 2026-10-04; [Bedrock streaming blog](https://tech.bedrockstreaming.com/2022/09/06/monitoring-at-scale-with-victoriametrics.html),
  accessed 2026-10-04). Grafana/Mimir's own issue tracker confirms downsampling exists "for fast
  results for long range" queries ([mimir#1834](https://github.com/grafana/mimir/discussions/1834),
  accessed 2026-10-04).
- Time-travel/as-of semantics are table stakes in analytical stores: Delta Lake time travel
  ([Microsoft Fabric docs](https://learn.microsoft.com/en-us/fabric/data-engineering/delta-lake-time-travel),
  accessed 2026-10-04); value-level **data diff** between two dataset versions is a product
  category of its own ([Datafold](https://www.datafold.com/data-diff/), accessed 2026-10-04;
  [open-source data-diff](https://www.datafold.com/blog/open-source-data-diff/), accessed 2026-10-04).
- Lineage systems record run- and dataset-version history precisely so "what changed and when" is
  answerable: Marquez API lists all dataset/job versions and lets you explore history to diagnose
  failures ([Marquez blog](https://marquezproject.ai/blog/using-marquez-api/), accessed 2026-10-04;
  [OpenLineage](https://openlineage.io/), accessed 2026-10-04). We already store the events
  (jobs, builds, incidents, cluster runs); we lack the *query shape* over time.

### 2.3 Download/export: boring, universal, and missing here

- Grafana's export flow is the canonical UX: hover panel → menu → **Inspect → data → Download
  CSV** ([Grafana blog, 2024](https://grafana.com/blog/how-to-export-any-grafana-visualization-to-a-csv-file-microsoft-excel-or-google-sheets/),
  accessed 2026-10-04). Table data exports are expected in every observability UI
  ([community thread](https://community.grafana.com/t/export-csv-data/2233), accessed 2026-10-04).
- Data platforms export in the format the *consumer* needs: Parquet for pipelines, CSV for humans,
  JSONL for machines ([BigQuery export docs](https://docs.cloud.google.com/bigquery/docs/exporting-data),
  accessed 2026-10-04; [PostHog file-download exports](https://posthog.com/docs/cdp/file-download-exports),
  accessed 2026-10-04; [Palantir Data Connection exports](https://palantir.com/docs/foundry/data-connection/export-overview/),
  accessed 2026-10-04).
- Bulk export must stream to avoid memory pressure (row-by-row, not materialize-then-send)
  ([Spring bulk-export pattern](https://medium.com/@AlexanderObregon/bulk-data-export-in-spring-boot-without-memory-pressure-0d85a96eeaa8),
  accessed 2026-10-04).

### 2.4 Alerting: delivery + dedupe + routing are the three known failure modes

- The delivery gap is the first one everyone lists: data observability tools "detect anomalies or
  quality violations in real time and immediately notify relevant team members through email,
  messaging platforms"
  ([Collate guide](https://www.getcollate.io/learning-center/data-observability-tools), accessed
  2026-10-04). Monte Carlo's webhook surface fires on incident lifecycle events (created,
  acknowledged, status updated, owner changed…) — not just "alert fired"
  ([Monte Carlo webhooks docs](https://docs.getmontecarlo.com/docs/webhooks), accessed 2026-10-04;
  [DataWorkers integration write-up](https://dataworkers.io/blog/data-workers-monte-carlo/),
  accessed 2026-10-04).
- **Alert fatigue is the counterweight**: dedupe, grouping related alerts into one incident,
  recovery thresholds, and routing by domain/owner are the standard mitigations
  ([Monte Carlo alert-fatigue post](https://montecarlo.ai/blog-alert-fatigue), accessed 2026-10-04;
  [Better Stack best practices](https://betterstack.com/community/guides/monitoring/best-practices-alert-fatigue/),
  accessed 2026-10-04; [Rootly alert management](https://rootly.com/alert-management/alert-management-best-practices),
  accessed 2026-10-04; [FireHydrant alert deduplication](https://docs.firehydrant.com/docs/alert-deduplication),
  accessed 2026-10-04).
- Routing by *domain* rather than pipeline, with thresholds reflecting business tolerance, is
  called out as the thing that makes alerting work
  ([DataForge observability-tools comparison](https://thedataforge.medium.com/data-observability-tools-monte-carlo-databand-openlineage-4875ba5510ad),
  accessed 2026-10-04).
- Fleet/product precedent for the channel mix: robot fleet dashboards route alerts to Slack,
  email, and webhooks ([Avala docs](https://avala.ai/docs/visualization/fleet/alerts-and-notifications),
  accessed 2026-10-04); webhooks with signatures are the integration primitive
  ([UptimeRobot](https://help.uptimerobot.com/en/articles/14498593-webhook-integration), accessed
  2026-10-04; [Hookdeck webhook observability](https://hookdeck.com/webhooks/guides/webhook-observability-architecture),
  accessed 2026-10-04).

### 2.5 Presentation: time range + annotations + export are the "nice history" pattern

- The universal observability UI pattern: a **time range picker** over persisted series, panel
  inspect/export (§2.3), and event annotations overlaying metrics
  ([Grafana time-series docs](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/time-series/),
  accessed 2026-10-04).
- Search/presentation separation (facets over a metadata index) is how curation surfaces stay
  usable at scale — NN/g: facets let users apply multiple filters simultaneously and are based on
  faceted taxonomies ([NN/g Taxonomy 101](https://www.nngroup.com/articles/taxonomy-101/),
  accessed 2026-10-04; [A List Apart faceted navigation](https://alistapart.com/article/design-patterns-faceted-navigation/),
  accessed 2026-10-04). Our episode index already has state/flag facets; history views should
  reuse the same vocabulary.

---

## 3. Gap inventory, ranked by (pain × elegance of fit)

| # | Gap | Pain today | Evidence | Fits engine because |
|---|---|---|---|---|
| G1 | **Metric/job history is unreadable** (tail-of-file, no rollups, no time range) | High — "can't check historic data nicely" | §1, §2.2 | The events are already written; only storage tiers + query shape are missing |
| G2 | **No download of anything** (one JSON manifest endpoint) | High — "can't download it" | §1, §2.3 | Streaming CSV/JSONL writer is stdlib; Parquet already in the dependency set via builds |
| G3 | **No alert delivery** (notify_preview sends nothing) | High — "can't alert people" | §1, §2.4 | ADR 0020 §10.3 deferred delivery on purpose (owner authorization); the missing piece is a delivery channel + dedupe policy, both patterned in §2.4 |
| G4 | **No diff/as-of over builds and manifests** | Medium — debugging "what changed between these two builds" is manual | §2.2 | Builds are content-addressed already; diff is a pure read over two stored manifests |
| G5 | **Snapshot analytics presentation** (no time range, no annotations on charts) | Medium | §2.5 | Sparklines/charts exist (`web/charts.py`); needs persisted rollups (G1) first |
| G6 | **No dataset-health trend** (quality/queue/incident rate over time) | Medium | §2.2 (validation-gate absence in the wild) | Composes G1 + G5 into the curation story |

---

## 4. Proposed extensions (elegant = smallest change that closes the gap)

### P1 — History: retention tiers over the JSONL sink (closes G1, enables G5/G6)
**Shape.** Keep the append-only JSONL sink as the hot tier (ADR 0017's "sink is the source of
truth" survives). Add a compaction step — on worker/API startup or `de` CLI — that folds records
older than N days into per-day rollup files (`var/metrics/rollups/YYYY-MM-DD.jsonl`) holding the
same p50/p95/p99 + bucketed-mean aggregates `aggregate.py` already computes. Reads become:
window < 24h → tail; longer → tail + rollups merged. No database table required; rollups are
plain files like `benchmarks/` and `experiments/` already are.
**Why elegant.** Reuses `summarize()`/`series()` unchanged; zero new dependencies; the historical
query is the same API with a wider `window_seconds`, so the OpenAPI contract grows by one optional
parameter (`since`/`until`) instead of a new subsystem. Matches the tiering pattern of §2.2 at
laptop scale.
**Tests.** Rollup idempotence, tail/rollup merge correctness at boundaries, empty-day handling,
compaction crash-safety (the ADR 0026 §5 transaction lesson).

### P2 — Downloads: `?format=csv|jsonl` on list endpoints + `Content-Disposition` (closes G2)
**Shape.** One shared streaming serializer (`csv` via stdlib, `jsonl` via `json.dumps` per row)
wired as an output option on the list endpoints (`/api/v1/jobs`, `/episodes`, `/artifacts`,
`/failures/episodes`, `/incidents`, `/contracts`, plus `/api/v1/metrics` series). Rows stream from
the catalog cursor — never materialize-then-send (§2.3). UI gets a "Download" link per table that
reuses the same query string the table was rendered with, so what you see is literally what you
get. Parquet export for episodes reuses the existing build/export machinery rather than a new
writer (the LeRobot-v3 export path already exists — ADR 0025).
**Why elegant.** The catalog already paginates with `before` cursors; export is the same read with
a different serializer. Contract grows by one query param per route; no new storage.
**Tests.** Row-count parity between HTML/JSON view and file; large-page streaming (no
materialization — assert bounded memory via generator usage); column-order stability (a CSV is a
contract too).

### P3 — Alert delivery: signed webhooks + digest, behind the existing notify gate (closes G3)
**Shape.** Implement the delivery path ADR 0020 §10.3 deliberately left open: a `Notifier` that
POSTs `notify_preview()`-shaped payloads to a configured webhook URL (Slack-compatible incoming
webhook, generic HTTP with HMAC signature), plus a daily digest mode. Delivery honors the
existing `notify_class` taxonomy and the alert budget (`monitor_suppressed_total` already counts
what dedupe hides). Owner authorization becomes configuration (`DE_NOTIFY_WEBHOOK` env var,
absent = dry-run exactly as today).
**Why elegant.** All the hard parts — detection, dedupe, budget, severity, notify class — exist
(ADR 0020). §2.4 says the failure modes are *fatigue and routing*, which our budget/dedupe/label
taxonomy already models; we add only the wire. Stdlib `urllib` + `hmac`, consistent with ADR 0014.
**Tests.** Signature verification round-trip; failed delivery never blocks the monitor tick (the
ADR 0008 lesson); digest grouping; suppressed incidents stay suppressed.

### P4 — Build/manifest diff and as-of views (closes G4)
**Shape.** `GET /api/v1/builds/{a}/diff/{b}` + a UI page over the two stored manifests: episodes
added/removed, profile-hash change, quality-stat deltas. As-of listing for episodes via the
build membership tables (already keyed by build hash). This is the Datafold-style value-level diff
(§2.2) at the only granularity that matters here — curated builds.
**Why elegant.** Pure function over two existing stored objects; no new writes. It is also the
natural "what changed" answer for the Traceplane failure class (silent schema/membership drift).

### P5 — History-aware presentation (closes G5/G6)
**Shape.** Time-range picker on Metrics/Status pages backed by P1 rollups; incident/cluster-run
events annotated on the charts (Grafana annotations pattern, §2.5); a "dataset health" page
stacking validation rate, quarantine rate, queue depth, and incident counts over the rollups.
Facet/time vocabulary shared with the episode index (§2.5).
**Why elegant.** Server-rendered, no JS (ADR 0026 §6 constraint) — a range picker is a form; the
charts module already draws from series data.

### Sequencing
P2 (downloads) is the cheapest win and unblocks user trust immediately. P3 (webhooks) closes the
most operationally dangerous gap. P1 (rollups) is the enabler for P5. P4 is independent and can
land anytime. None of these violate ADR 0012 (no cloud) or ADR 0014 (dependency-free core).

### What we should NOT build
- A metrics database (Prometheus/VictoriaMetrics/Mimir): §2.2's tools solve fleet scale we do not
  have; a JSONL + rollup tier answers the same questions at our scale without an operator service
  (ADR 0017's "no metrics server, no scraper" explicitly chose this).
- A general lineage graph service (Marquez/OpenLineage full stack): our lineage is a DAG of jobs →
  episodes → builds that already fits in the catalog; P4 gives the queryable part.
- Email delivery first: webhook covers Slack/most routing surfaces with one integration and no
  SMTP credential handling (secret-management concern, CLAUDE.md non-negotiables).

---

## 5. Falsifiers

- If rollup queries are still slow or lossy on a 30-day sink in the drill, the file-tier approach
  is wrong — then and only then evaluate an embedded columnar store (DuckDB-style) as a read
  cache over the same JSONL (still no server).
- If users export less than expected after P2 ships (instrument `api_requests_total` by route —
  the metric already exists), downloads were a stated-but-not-felt need and P5 should move ahead
  of P4.
- If webhook delivery produces alert fatigue in practice (suppressed/total ratio rising), the
  budget policy needs tuning before more channels are added — never add email on top of a noisy
  webhook.

---

## 6. Sources

| # | Source | URL | Accessed | Retrieval |
|---|---|---|---|---|
| 1 | Jump Capital, Robotics Is Entering Its Operations Era | https://jumpcap.com/insights/robotics-is-entering-its-operations-era/ | 2026-10-04 | snippet |
| 2 | BusinessWire, Foxglove Unified Data Search & Curation launch | https://www.businesswire.com/news/home/20260421818840/en/Foxglove-Launches-Unified-Data-Search-and-Curation-Platform-to-Accelerate-Physical-AI-Development | 2026-10-04 | snippet |
| 3 | SCSP ISF Voices 2026, The Robotics Data Gap | https://scsp222.substack.com/p/isf-voices-2026-the-robotics-data | 2026-10-04 | snippet |
| 3b | Foxglove blog, Data Search & Curation | https://foxglove.dev/blog/data-search-curation | 2026-10-04 | snippet |
| 4 | IBM, The data gap that's holding back robotics | https://www.ibm.com/think/news/the-data-gap-holding-back-robotics | 2026-10-04 | snippet |
| 5 | Traceplane, We Audited 10 Popular Open-Source Robot Datasets | https://traceplane.ai/blog/we-audited-10-robotics-datasets | 2026-10-04 | fetched |
| 6 | VictoriaMetrics single-server docs (retention) | https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/ | 2026-10-04 | snippet |
| 7 | Bedrock, Monitoring at scale with VictoriaMetrics | https://tech.bedrockstreaming.com/2022/09/06/monitoring-at-scale-with-victoriametrics.html | 2026-10-04 | snippet |
| 8 | Grafana Mimir discussion #1834 (downsampling for long ranges) | https://github.com/grafana/mimir/discussions/1834 | 2026-10-04 | snippet |
| 9 | Microsoft Fabric, Delta Lake time travel | https://learn.microsoft.com/en-us/fabric/data-engineering/delta-lake-time-travel | 2026-10-04 | snippet |
| 10 | Datafold, Data Diff | https://www.datafold.com/data-diff/ | 2026-10-04 | snippet |
| 11 | Datafold, open-source data-diff | https://www.datafold.com/blog/open-source-data-diff/ | 2026-10-04 | snippet |
| 12 | Marquez blog, Exploring the Marquez Lineage API | https://marquezproject.ai/blog/using-marquez-api/ | 2026-10-04 | snippet |
| 13 | OpenLineage | https://openlineage.io/ | 2026-10-04 | snippet |
| 14 | Grafana blog, Export any visualization to CSV | https://grafana.com/blog/how-to-export-any-grafana-visualization-to-a-csv-file-microsoft-excel-or-google-sheets/ | 2026-10-04 | snippet |
| 15 | Grafana community, Export CSV data | https://community.grafana.com/t/export-csv-data/2233 | 2026-10-04 | snippet |
| 16 | BigQuery, Exporting table data | https://docs.cloud.google.com/bigquery/docs/exporting-data | 2026-10-04 | snippet |
| 17 | PostHog, File download exports | https://posthog.com/docs/cdp/file-download-exports | 2026-10-04 | snippet |
| 18 | Palantir Foundry, Data Connection exports | https://palantir.com/docs/foundry/data-connection/export-overview/ | 2026-10-04 | snippet |
| 19 | Obregon, Bulk Data Export Without Memory Pressure | https://medium.com/@AlexanderObregon/bulk-data-export-in-spring-boot-without-memory-pressure-0d85a96eeaa8 | 2026-10-04 | snippet |
| 20 | Collate, Data Observability Tools guide | https://www.getcollate.io/learning-center/data-observability-tools | 2026-10-04 | snippet |
| 21 | Monte Carlo, Webhooks docs | https://docs.getmontecarlo.com/docs/webhooks | 2026-10-04 | snippet |
| 22 | DataWorkers, Monte Carlo alert lifecycle integration | https://dataworkers.io/blog/data-workers-monte-carlo/ | 2026-10-04 | snippet |
| 23 | Monte Carlo, Alert Fatigue | https://montecarlo.ai/blog-alert-fatigue | 2026-10-04 | snippet |
| 24 | Better Stack, Preventing Alert Fatigue | https://betterstack.com/community/guides/monitoring/best-practices-alert-fatigue/ | 2026-10-04 | snippet |
| 25 | Rootly, Alert Management Best Practices | https://rootly.com/alert-management/alert-management-best-practices | 2026-10-04 | snippet |
| 26 | FireHydrant, Alert Deduplication | https://docs.firehydrant.com/docs/alert-deduplication | 2026-10-04 | snippet |
| 27 | DataForge, Observability Tools comparison (routing by domain) | https://thedataforge.medium.com/data-observability-tools-monte-carlo-databand-openlineage-4875ba5510ad | 2026-10-04 | snippet |
| 28 | Avala, Fleet Alerts & Notifications | https://avala.ai/docs/visualization/fleet/alerts-and-notifications | 2026-10-04 | snippet |
| 29 | UptimeRobot, Webhook Integration | https://help.uptimerobot.com/en/articles/14498593-webhook-integration | 2026-10-04 | snippet |
| 30 | Hookdeck, Webhook Observability Architecture | https://hookdeck.com/webhooks/guides/webhook-observability-architecture | 2026-10-04 | snippet |
| 31 | Grafana docs, Time series visualization | https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/time-series/ | 2026-10-04 | snippet |
| 32 | NN/g, Taxonomy 101 (facets) | https://www.nngroup.com/articles/taxonomy-101/ | 2026-10-04 | snippet |
| 33 | A List Apart, Design Patterns: Faceted Navigation | https://alistapart.com/article/design-patterns-faceted-navigation/ | 2026-10-04 | snippet |

Internal evidence: `agents/observability/conventions.md`, `src/data_engine/observability/aggregate.py`
(read-back contract), `src/data_engine/monitoring/service.py` (`notify_preview`, ADR 0020 §10.3),
`agents/decisions/0012` (no cloud), `agents/decisions/0014` (dependency-free core),
`agents/decisions/0017` (sink-as-truth), `agents/decisions/0025` (LeRobot-v3 export).
