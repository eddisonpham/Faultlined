# 0024. The visual layer is server-rendered SVG over data the product already collects

- **Status:** accepted
- **Date (UTC):** 2026-09-30
- **Deciders:** owner + implementer agent
- **Amends:** [ADR 0014](0014-minimal-ui-server-rendered.md) (no bundler) and
  [ADR 0021](0021-frontend-instrument-pass.md) (one client runtime). Extends
  [ADR 0017](0017-runtime-metrics-aggregation.md), whose series this surface finally draws.

## Context

The review in `reviews/2026-09-30-metrics-and-visualization-assessment.md` found the product's real
weakness. It is not missing capability — three readers, content-addressed builds, quarantine,
lineage, eleven runtime metric series and a per-episode quality record all work. It is that the
operator cannot see any of it, and in two places the product publishes a number that does not mean
what its name suggests.

**The curation chart is meaningless.** `/ui/insights` scatters episodes by `movement_score`, which
is the mean L2 norm of frame-to-frame deltas *in raw units*. One real episode in the reference run
is a driving dataset in millimetres and reads `100015978.5`; the arm episodes read `3.54`, `2.31`,
`0.16` and `0.022`. On a linear axis four of five episodes are indistinguishable points on the
floor. The chart is not misdrawn — it is drawing a quantity with no shared unit and calling it
speed. This must be fixed *before* it is charted, or the next stage ships a broken chart with a
confident appearance.

**The collected data is already collected.** ADR 0017 buckets eleven series by minute —
`api_request_duration_seconds`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`,
`pipeline_stage_duration_seconds`, `episodes_ingested_total`, `jobs_queue_depth` and others. The
metrics page renders each as a 44-pixel `<polyline>` with no axes, no units, no scale and no way to
ask which jobs produced a point. The expensive part is finished; only the presentation is missing.

**Three structures have no picture.** The catalog is 14 tables with real foreign keys; the API is 20+
endpoints; lineage is a genuine DAG (episode → build → artifact, plus validation and job edges) that
is rendered as a table of rows. An operator onboarding to the system, or reviewing a PR that adds a
column, has no way to see the shape of the thing they are changing.

**The temporal signal is a word where it should be a picture.** ADR 0023 gave every episode an
`integrity` verdict. "gapped" tells an operator that something is wrong and nothing about where. The
MCAP reader already keeps a bounded, contiguous, true-resolution window of the log; the per-frame
motion magnitude for that same window is free, and it renders a dropped recording as a literal hole
in a line — the way every monitoring tool an operator has ever used shows a dropout.

## Options considered

### How to draw

| Option | Pros | Cons |
|---|---|---|
| **Server-rendered inline SVG, extended (chosen)** | No dependency, no CDN, no bundler, no build step; charts are testable as strings; works with JS disabled; the idiom is already established by `_sparkline` / `_histogram_svg` / `_scatter_svg` in ADR 0021 | No zoom, pan or brushing; hit-testing for drilldown is hand-written |
| Vendored uPlot or Chart.js as a static asset | Real interaction, tooltips, brushing for free | A third-party dependency to justify in the technology matrix and keep current, for a local-first tool with no network; ADR 0021's instrument pass exists precisely to keep the client runtime minimal |
| CDN charting library | Simplest possible | Breaks the offline promise outright and fails the no-CDN hygiene rule |
| Client-side rendering in vanilla JS | Full interactivity | Reintroduces a second source of truth for the data, which is how a UI starts disagreeing with its own API |

### What to draw the schema from

| Option | Pros | Cons |
|---|---|---|
| **Introspect the live database (chosen)** | Cannot drift; shows the constraints the code actually has; a column added without a migration is visible immediately | Depends on a reachable catalog, so it needs an honest empty state |
| A hand-written diagram in `architecture/` | No database needed; reads well in review | Silently disagrees with the code, which is the failure mode this project exists to prevent, and nobody notices until it is load-bearing |
| Generated from the Pydantic models | Covers the API, not the catalog | The catalog is the part with foreign keys and the part nobody documents |

### What to add to the analysis

| Option | Pros | Cons |
|---|---|---|
| **Scale-free population score, derived from what `analyze` already computes (chosen)** | `mean_abs_delta_norm` is already computed per dimension; the population version is the same arithmetic one level up, and it is comparable across datasets by construction | Two numbers where there was one, and the UI has to say which is which |
| Normalize `movement_score` in place | One number | Destroys the raw magnitude, which is genuinely useful when you want to know how much a joint actually moved in its own units |
| Leave it and label the axis "raw units" | Free | Leaves the product's primary curation chart unusable, which is the problem |

## Decision

1. **Charts are inline SVG built in Python**, extending the existing helpers into a small chart
   module with axes, unit labels, quantile bands and a shared scale. A **log scale is chosen
   automatically when a series spans more than two orders of magnitude**, because the reference run
   spans eight — a linear axis is not a rendering choice there, it is a way of showing nothing.
   Every chart is `role="img"` with a real `aria-label` and carries its own numbers in text, so the
   page is readable with the images suppressed.
2. **A scale-free `normalized_speed` is published beside the raw `movement_score`**, not instead of
   it. It is the mean over judged dimensions of `mean_abs_delta_norm` — dimensionless, so radians
   and millimetres are comparable — and it is what the curation scatter and the speed distribution
   plot. The raw score stays available, labelled with its unit, for "how far did this joint actually
   travel".
3. **`/ui/observability`** charts the ADR 0017 series with real axes, and every plotted point
   drills down to the jobs or episodes in that bucket. An operator looking at a latency spike gets
   to the run that caused it without leaving the page.
4. **`/ui/schema`** reads the live catalog: tables, columns, types, nullability, row counts, and the
   foreign-key graph drawn from the real constraints, with the ingest and build paths overlaid. Row
   counts are live, so the page doubles as a "what is actually in here" view.
5. **Lineage is drawn as a DAG** on the build and episode detail pages, from the existing reverse
   and forward endpoints. Nodes are links; the graph is the navigation.
6. **A bounded motion trace per episode** so temporal integrity is visible. Stored decimated and
   bounded exactly like the reader's existing window, so it cannot become an O(dataset) cost.
7. **Recording reliability is aggregated**: episodes gapped over total, per format and per robot, on
   the same page as the episode list. A dataset assembled from gappy recordings is a dataset with
   holes, and nothing in the pipeline notices because nothing counts.

8. **The motion trace (decision 6) is `jerk` per transition, and a transition that spans a
   dropout is not drawn** (added 2026-09-30, during implementation). The plotted value is the
   judging dimensions' mean `|delta| / range` per frame - the very quantity `jerk_score`
   averages, so chart and score cannot disagree about what motion is. `|delta|` across a 30 s
   hole is two poses, not a velocity, so that transition becomes the hole itself: runs break at
   dropouts and the line is drawn as separate segments with the gap's measured width. The trace
   is stored as runs of `(seconds since start, score)`, capped at `TRACE_POINTS` (240) with
   endpoints preserved, and it exists only when the clock is trustworthy - a backwards or
   absent clock yields no trace rather than a fiction.
9. **Chart x is mapped by value, not by index** (added 2026-09-30). `polyline_points` placed
   points at `x / (count - 1)`, which is right for evenly bucketed series and wrong for real
   time: a 30 s hole occupied the same width as a 100 ms blip. Points now map over the drawn
   domain, which is pixel-identical for indexed input (the metrics buckets) and honest for the
   trace. This is a correction to the primitive, not a new one, and the indexed tests pin the
   old pixels to prove nothing else moved.

## Consequences

- No new dependency, no CDN, no bundler, no build step. The entire visual layer is stdlib string
  building, and the repo's dependency list does not change.
- The chart primitives are pure functions from data to strings, so they are unit-testable without a
  browser: a test asserts the path coordinates, the tick labels and the empty state, not a
  screenshot. This is the property that makes the surface maintainable.
- Log scale is a real behaviour with a real failure mode — a series containing zero or negative
  values cannot be logged. It falls back to linear, and there is a test for the fallback.
- `normalized_speed` is a new column on `episode_quality` and a new field on two API schemas, so the
  OpenAPI contract is regenerated. Episodes ingested before it get `null`, and the UI says so rather
  than drawing a zero.
- Introspecting the live catalog means `/ui/schema` needs the database up. It renders an explicit
  "catalog unreachable" state instead of an error page, because a schema view that 500s is worse
  than no schema view.
- The motion trace is a per-episode array. It is bounded and decimated at write time, but it is the
  first per-episode payload the catalog stores that is not a scalar, and the size discipline in
  `architecture/storage.md` applies to it.
- Everything in this ADR is presentation except decisions 2, 6 and 7, which touch the catalog and
  therefore carry the same no-re-read and O(one episode) constraints as ADR 0018.
