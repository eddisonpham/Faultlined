# ADR 0030: Streamed downloads are the same read with a different serializer

- **Date:** 2026-10-04
- **Status:** accepted
- **Stage:** post-stage-5 operator-convenience work
  ([implementation plan](../implementation/vocabulary-and-operator-gaps-plan.md) B1, closes gap G2
  in [infrastructure-gaps.md](../research/infrastructure-gaps.md))
- **Related:** [ADR 0009](0009-api-style.md) (HTTP+JSON API), [ADR 0014](0014-minimal-ui-server-rendered.md)
  (no new dependencies), [ADR 0025](0025-lerobot-v3-export-of-builds.md) (materialized exports are a
  build concern), [ADR 0017](0017-runtime-metrics-aggregation.md) (metrics reads)

## Context

Operators can filter every table in the UI and read the same rows through the JSON API, but they
cannot get the data *out*: there is no CSV, no JSONL, nothing with a `Content-Disposition`. The
only export in the product is `GET /api/v1/episodes/export` (a JSON manifest) and the LeRobot v3
build export (a whole dataset directory). The gap is the everyday one — "I filtered this table,
now give me these rows in a file" — and the industry pattern is uniform: exports are the query you
already ran, serialized differently (Grafana's inspect→CSV, BigQuery/PostHog export links).

Every list read already paginates on a `created_at` cursor, so the rows exist; only the
serializer is missing. The constraints: no new dependency (ADR 0014), no export service that can
drift from the live views, and a CSV is contract surface like any other output.

## Decision

1. **An export is the same read with a different serializer.** `?format=csv|jsonl` on the list
   routes (`/api/v1/jobs`, `/episodes`, `/artifacts`, `/failures/episodes`, `/incidents`,
   `/contracts`, and `/metrics` for the sparkline series) returns the filtered rows as a file
   download. There is no export service, no export job, and no materialized export artifact:
   what a download produces is bytes in a response, computed from the catalog at request time.

2. **Rows stream.** The shared serializer (`api/download.py`) walks the cursor one page at a
   time and serializes rows as they are pulled; nothing accumulates the result set. The first
   row is pulled before the response starts streaming, so a catalog failure is a normal JSON
   error rather than a half-written file. A single download is bounded at 50,000 rows — a
   runaway export is an outage with a progress bar; the JSON cursor remains available beyond it.

3. **The CSV header is the response model's field order.** Each row is normalized through the
   same Pydantic item model the JSON view validates with, so a CSV row and a JSON item carry
   identical values in identical shapes. A CSV is a contract too: the header is pinned by test.

4. **What you see is what you get.** The UI's Download links reuse the exact query string the
   table was rendered with. Structured values (lists, objects) stay in one CSV cell as their JSON
   form rather than being exploded into columns nobody agreed on.

5. **Downloads order newest-first through the cursor path.** For the episode flag views
   (which rank by quality signal), the download pages on `created_at` instead — a signal ranking
   cannot page consistently and would duplicate rows across page boundaries. Membership is
   identical to the view; only the order differs.

6. **Parquet is not a download.** Materialized dataset export stays the build's LeRobot v3
   export (ADR 0025). A per-table Parquet writer would be a second export mechanism for rows
   that already reach the user as CSV/JSONL.

## Alternatives considered

- **A materialized export step (export job writes a file to disk).** Rejected: it introduces an
  artifact that can drift from the live view, needs storage and GC (already an open gap), and
  answers "give me these rows now" with "come back when the job finishes".
- **A dedicated `/export` route family per table.** Rejected: duplicates pagination, filters and
  serialization per table, and the two paths drift the moment a filter is added to one.
- **Parquet/pandas for every download.** Rejected: new dependency (ADR 0014) for rows an
  operator opens in a spreadsheet or pipes to `jq`.

## Consequences

- The OpenAPI contract grows one optional `format` parameter per list route; the committed
  contract regenerates in the same commit.
- `catalog.list_episodes` gained a `before` cursor (newest-first ordering) alongside the flag
  rankings; the UI view behavior is unchanged.
- Every new list endpoint is expected to carry the same `format` parameter; the shared
  serializer is the only serialization code path.
- A row's CSV cell for a structured value is JSON text; anything parsing these CSVs must handle
  quoted JSON in a cell.

## Docs updated

- [api.md](../architecture/api.md) (Conventions: downloads)
- [implementation plan](../implementation/vocabulary-and-operator-gaps-plan.md) (execution record)
