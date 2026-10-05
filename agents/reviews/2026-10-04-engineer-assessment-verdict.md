# Review: whole-program assessment as an incoming robotics-ML engineer — 2026-10-04

- **Reviewer role:** Independent assessment, written from outside the implementing team's context. Standing in for a
  Meta / NVIDIA / Tesla-style robotics-ML platform engineer who has to decide whether to run their team's data on
  this.
- **Commit reviewed:** `bab9ab5` (clean tree)
- **Stage claimed:** Performance/Scaling closed, Production Hardening in progress
- **Evidence:** [EXP-0014](../experiments/0014-foreign-data-ingest-corpus.md), [EXP-0015](../experiments/0015-feature-latency-at-three-scales.md),
  [EXP-0016](../experiments/0016-concurrent-workflow-contention.md), [EXP-0017](../experiments/0017-operator-click-and-attention-budget.md).
  Every number below is measured; the four scripts that produced them are committed.

---

## 1. The verdict, up front

**The engine underneath is good. The product on top of it is half-built. Today an operator can put data in and
nothing else.**

The catalog, the content addressing, the lineage model, the job lifecycle, the queue, the worker, the observability
and the determinism discipline are all better than most things I have seen at this size. Under concurrent load the
system does not degrade: ingest throughput with four actors competing was within 1 % of ingest throughput alone, with
zero errors and zero lost writes. That is a real result and it is the strongest thing in the repository.

But the product's stated purpose — hand reproducible LeRobot v3 dataset builds to a training script — **cannot be
completed from the browser**. Validate, build, export and save-slice all exist in the JSON API and none of them has a
UI route. An operator gets data in, curates the task strings, and then has to leave the tool to finish their job.

There is a second, quieter version of the same problem. **The monitoring subsystem never runs.** Eleven rules,
control limits, incidents, severities, an alert budget, a notifier and an entire `/ui/incidents` page exist, and
`MonitorService.tick()` has exactly two callers in the codebase: the HTTP endpoint and the test suite. Nothing
schedules it. In a running deployment the incident queue is empty forever, and the page that exists to tell you
something is wrong is structurally incapable of ever showing you anything.

Neither of these is visible to any gate in the repository. That is the finding I would lead with: **the test suite,
the coverage floor, the contract drift check and the browser audit all pass, and none of them can see a missing
route or a missing scheduler.**

---

## 2. What the measurements found

### Good, and worth defending publicly

- **No contention cliff.** [EXP-0016](../experiments/0016-concurrent-workflow-contention.md): ingest at 3.54 jobs/s
  alone, 3.58 jobs/s with a reader, a curator and the monitor all running. Read p50 79.8 ms alone, 55.3 ms under load.
  Zero 5xx, zero stalled jobs, zero lost vocabulary writes across 500+ concurrent mutations. **Do not add workers, a
  broker, or sharding.** There is no evidence they are needed and the current design point is correct.
- **The floor is ~65–80 ms and it is framework overhead, not Postgres.** Twenty routes sit there regardless of catalog
  size, and several get *faster* from 20 to 1,000 episodes. Any future latency work should ignore this floor.
- **Refusals are honest.** Ten of nineteen foreign fixtures were refused with
  `no reader recognises <path> (tried: mcap, lerobot)`. That is the design working: a gap that is *named* costs
  nothing, because the operator knows exactly what happened.
- **The vocabulary work is real.** The per-string advisory locking, the stale-undo refusal, and the compensating
  events are the right answers to the wrong answers a curation tool usually gives. Under the concurrent curator load
  in EXP-0016 nothing was lost.
- **The determinism discipline is genuine** and rare: content addressing, canonical manifests, byte-identical
  rebuilds, no model anywhere in a decision path, baselines refused without authorisation.

### Bad, in priority order

| # | Severity | Finding | Evidence |
|---|---|---|---|
| **1** | **blocker** | **The browser cannot validate, build, export or save a slice.** `POST /ui/jobs` emits only `ingest` and `ingest_source`. The product's stated output is unreachable from its own UI. | EXP-0017 |
| **2** | **blocker** | **The monitor never runs.** `tick()` is called only by its own HTTP route and by tests. The worker loop and the FastAPI lifespan never call it. The incident queue is empty forever. | EXP-0016 |
| **3** | **major** | **A topic named `...grip...` silently voids an episode's quality verdict.** `_GRIPPER = re.compile("grip")` matches against `<topic>.<path>`, and `_scorable` picks the busiest multi-dim topic. Byte-identical payloads give `smooth` as `/joint_states` and `unknown` as `/left_gripper/joint_states`. The episode ingests successfully and carries no verdict, with nothing explaining why. | EXP-0014 D1 |
| **4** | **major** | **One non-finite value fails the whole episode, three times over.** `ChannelStats` has no finite guard, so `max = Infinity` and the `jsonb` insert raises `InvalidTextRepresentation`. It is classified retryable, so the same file is re-read three times. ADR 0023 claims this is handled; it is handled inside `analyze()` and not in the reader. | EXP-0014 D2 |
| **5** | **major** | **The operator is told `job handler failed`.** The real reason is logged and never persisted, so the Jobs page, the API and the Failures page all show a constant. For a tool whose stated value is failing honestly, the one question an operator has is unanswerable. | EXP-0014 D3 |
| **6** | **major** | **`/ui/vocabulary` duplicates its two most expensive queries.** `vocabulary_health()` internally re-runs `list_unmapped(limit=5000)` and `list_entries()`, which the page has already run. 55 % of the page's time is redundant, and the redundant part scans 100× more rows than the page above it. 595 ms p95 at 10k episodes. | EXP-0015 |
| **7** | **major** | **Two routes blow the payload, not the query.** `GET /api/v1/quality/summary` returns **2.2 MiB of JSON** for a summary; `/ui/insights` returns **5.2 MiB of HTML** for a page. Both 390–400 ms p95 against a 200 ms target, and the seeded metadata is smaller than a real episode's, so those are floors. | EXP-0015 |
| **8** | **major** | **The primary robot data format has no reader.** `ros2 bag record`'s default output is a sqlite3 file; the engine reads MCAP, which is what `ros2 bag *convert*` writes. Of eleven layouts an operator arrives with, the engine reads two. | EXP-0014 |
| **9** | **minor** | **A `.zip` containing a readable bag is refused.** Recordings arrive as zips. | EXP-0014 |
| **10** | **minor** | **An empty MCAP ingests successfully as a zero-frame episode.** "The recorder never started" and "here is an episode" should not be the same outcome. | EXP-0014 |
| **11** | **minor** | **The frozen cluster archive is not frozen at read time.** `GET /api/v1/clusters` is a live computation over every episode's extracted core, 417 ms at 10k, while every mutation on it correctly returns 410. | EXP-0015 |

### Two defects in the measurement tooling itself, found and fixed

Recording these because they are the same class of error the repository already has a lesson about.

- `scripts/scale_campaign.py::_seed_catalog` wrote `motion_trace` as a flat list of floats; the real shape is a list
  of *runs* of pairs. **Every episode that campaign had ever seeded would 500 on its detail page.** It went unnoticed
  because the campaign only ever measured repository queries, never a rendered page. Fixed.
- `scripts/feature_latency.py::seed_vocabulary` called two keyword-only functions positionally and swallowed the
  `TypeError`, so the first full latency run measured `/ui/vocabulary` and `/api/v1/vocabulary` **with an empty
  vocabulary**. Fixed; the campaign was re-run and the numbers above are from the corrected run.

---

## 3. Limitations, stated plainly

**Of the product.** It cannot read the format most robots actually produce by default. It cannot finish a job from
its own UI. Its monitoring does not monitor. Its failure messages are constant. Its two slowest pages are slow for
structural reasons that no amount of indexing will fix. It has no video support, so on a modern robot — where video
is where the bytes are — it describes metadata and ignores the data.

**Of the measurements.** All synthetic, one machine, Windows, no committed baseline. The seeded `episode_metadata` is
smaller than a real MCAP episode's, so every payload figure is a floor. The seeded episodes are not real
distributions of task strings, robot types or failures, so the *content* of `/ui/insights` and `/ui/vocabulary` is
representative in shape but not in realism. No foreign-format throughput number exists, because no foreign format is
read. Read and monitor legs have 15 and 9 samples; their p95 is indicative. Nothing here runs at 100k episodes.

**Of the record.** No human was observed using the product. EXP-0017 measures the *designed* cost of a workflow by
reading served HTML, not the variance in how long a real person takes. No screen reader was used. No colour-blindness
check with an operator.

**Still unmeasured from the existing backlog, and I am not going to pretend otherwise.** The unmapped-share
falsifier and the candidate-precision falsifier (plan §4) are still unmeasured. There is still no detection-accuracy
figure for the notifier. There is still no baseline for the run-intelligence workloads. The 50 MB/s ingest target
remains unreachable by design and has not been re-scoped.

---

## 4. What to keep, what to cut

**Keep, and treat as the core.**

- The catalog and the content-addressed store. Correct, boring, and the reason any of this is worth building.
- The job lifecycle with retries, deadlines, cooperative cancel, dead-worker recovery and failure classification.
  Genuinely more careful than most.
- The vocabulary (ADR 0029): content-derived ids, human-approved labels, the unmapped queue as a *query* rather than
  a table, and events with exact compensating undo. The concept is right and the concurrency work on it was real.
- Deterministic monitoring as a *design*, and the refusal to put a model in the detection path. The missing scheduler
  is an omission, not a wrong idea — and the fix is about twenty lines.
- The experimental discipline. Exp records with provenance, honest "no figure yet", baselines behind an owner flag.
  This is rarer and more valuable than most of the features.

**Fix immediately (days).** Items 3, 4, 5, and the seeding bugs. Each is small, each is a correctness or honesty
defect, and each is currently live.

**Fix next (a sprint).** Item 1 and item 2. Between them they are the difference between a demo and a product.

**Then (a quarter).** Items 6, 7 — pure engineering, well understood, measurable before and after.

**Cut or formally defer.**

- **The cluster proposal surface.** Reads cost 417 ms at 10k, mutations are 410, and the vocabulary has replaced it.
  Freeze the reads as static output or drop the routes; carrying a live computation for a frozen archive is cost
  without product.
- **`/ui/benchmarks` and `/ui/experiments`.** These render the repository into itself. Both are under 35 ms and are
  the two cleanest pages in the product, and neither is used by anybody who was going to do their job. Keep them as
  developer conveniences; stop treating them as product surface in the README.
- **The provisional 50 MB/s ingest target.** It has been known-unreachable since EXP-0005. Re-scope it to per-stage
  numbers or delete it. A target the design cannot hit teaches everyone to ignore targets.

**Explicitly do not build.** Video decoding. Object-store or multi-node. A telemetry service. A frontend framework.
A queue broker. Each has been deferred for a stated reason and none of the new evidence here argues for changing
those answers.

---

## 5. Prioritised plan

Ordered by "unblocks a person doing their job", not by effort.

### P0 — one sprint, no design decisions required

1. **Persist the real failure reason.** Store the handler's message on the job row. One line in `jobs/worker.py:482`.
   Nothing downstream changes; every error page gets better immediately.
2. **Guard non-finite channel statistics** in the reader, so an `Infinity` is counted and reported the way ADR 0023
   already describes, and `InvalidTextRepresentation` is classified terminal. One guard plus one classification.
3. **Stop the topic name deciding the verdict.** Apply the gripper exclusion to the *leaf* dimension name, not to
   `<topic>.<path>`; and when `_scorable` selects a topic whose dimensions are all excluded, fall back to the next
   candidate rather than returning `unknown`. The `unknown` verdict should require that there was genuinely nothing
   to judge.
4. **Fix the two seeding bugs** (done in this commit) and add a guard so a seeding helper cannot report success on
   zero rows.
5. **Refuse a zero-message MCAP** as an episode. It is a recorder that never started, and "ingested successfully" is
   the wrong word for it.

### P1 — the two that decide whether this is a product

6. **Give the UI the rest of the pipeline.** `POST /ui/builds` and `POST /ui/validate` and `POST /ui/slices`, built
   through the same Pydantic models the JSON endpoints use, exactly as `POST /ui/jobs` already does. This is the
   single highest-value change available and it reuses a pattern the codebase has already established. An ADR is
   needed because it extends the UI mutation surface beyond what ADR 0014 enumerated.
7. **Schedule the monitor.** One call to `tick()` in the worker loop on an interval, next to the existing reaper and
   host sampler, with the cadence as a constant and a test asserting the loop ticks. Until this exists, ADR 0020 and
   the whole Incidents page are a design that has never run. This also needs an ADR, because it changes what
   "monitoring" means for a deployment.

### P2 — engineering, measurable, no product decisions

8. **Kill the duplicate work on `/ui/vocabulary`.** Compute `health` from the data the page already has, or give it
   an explicit small limit. Expect 595 ms → ~330 ms with no behavioural change.
9. **Bound the two payloads.** `quality/summary` returning 2.2 MiB for a summary needs a shape decision, not a
   query change. `/ui/insights` returning 5.2 MiB of HTML needs a cap on rows or a paged variant. Both need ADRs
   because they change a response contract.
10. **Give the frozen cluster reads a real answer.** Either serve them from stored output or drop the routes. 417 ms
    for an archive is not defensible.

### P3 — the format boundary, and the only place I would spend real money

11. **A ROS 2 sqlite3 bag reader.** This is the default output of `ros2 bag record`. It needs a CDR decoder for a
    small set of standard message types (`sensor_msgs/msg/JointState`, `sensor_msgs/msg/Imu`, `std_msgs/...`), which
    is a bounded, well-specified piece of work — and unlike a model, it can be tested against real bags.
    **The registry already makes this a registration, not a core change** (`api.md` §reader protocol, honoured by
    ADR 0022). This is the highest-value format work by a wide margin.
12. **CDR decoding inside the existing MCAP reader.** Today a `cdr` bag ingests "successfully" with **zero decoded
    channels and no quality verdict** — which is closer to silently-wrong than to support. Either decode the common
    types or make a bag with no decodable channels a visible, non-successful outcome. The second is a day of work and
    removes the worst outcome on this list.
13. **Unzip, cheaply.** Detect a zip container, extract to a temp path, read the first recognised member. Recordings
    arrive as zips.
14. **HDF5, if and only if there is a user.** UMI-style HDF5 is common in the literature, but the generator for it
    already had to pull in `h5py` as an extra, and no real user has asked. This one needs the owner's decision, not
    an engineer's.

### P4 — what would change my mind about the whole shape

15. **If task-string curation is not the bottleneck, the vocabulary is over-built.** Everything in Track A and Track B
    optimises the moment between "data arrives" and "task strings are named". That is the right bet for
    operator-driven curation and the wrong bet for a team that has already solved task labelling upstream. **The
    unmapped-share falsifier (plan §4) decides this and it is still unmeasured.** Measure it on a real corpus before
    spending another slice on Track B.
16. **If builds are made upstream, the engine is over-built in the other direction.** Content addressing, lineage and
    reproducible manifests are the right answer to "where did this dataset come from", and unnecessary if the answer
    lives in the training script's config.

---

## 6. Checklist

- [x] Architecture docs match the code — with **three exceptions this review found**: ADR 0023's non-finite claim
      does not hold for `ChannelStats` (D2); the ADR 0029 cluster freeze implies cheap reads it does not deliver
      (#11); ADR 0020's monitoring design implies a schedule nothing implements (#2).
- [x] Every significant decision has an ADR — yes, and the numbering is disciplined. The failures are omissions from
      the implementation, not from the decision record.
- [x] Interfaces match `architecture/api.md` — yes. The UI surface is the divergence, not the API.
- [x] No dead code, no speculative abstraction — **one exception**: `catalog/clusters.py` is retained in full behind a
      frozen surface that costs 417 ms to read. A6 already plans to trim it.
- [x] Dependencies justified — yes. Nothing here needed a new one; `h5py` stayed a generator-only extra.
- [x] `just ci` green — yes, at `bab9ab5` and on the tree this review commits.
- [x] Coverage gate enforced and not lowered — yes, 70 % floor, 89.99 % actual.
- [x] Benchmarks follow methodology — **with the caveats in §3**: no committed baseline for the run-intelligence
      workloads, and the seeded metadata is not a realistic size distribution.
- [x] Docs current; CLAUDE.md pointers valid — yes, at the commit reviewed.
- [ ] **Every shipped capability has an execution path** — **fails.** Items 1 and 2. This is the new checklist item,
      and it is the one that would have caught both.

## 7. Verdict

**accept with follow-ups**, on one condition: the P1 items — the UI path to build and export, and the monitor
schedule — are treated as release blockers rather than backlog entries. Without them the repository is a strong
data engine with an unfinished product on top, and the README currently describes the finished one.

Everything in P0 is a small, unambiguous fix to a real defect. Everything in P2 is engineering the team has already
proved it can do — this repository has now found and fixed four N+1s and one concurrency defect by measurement, and
the pattern works.