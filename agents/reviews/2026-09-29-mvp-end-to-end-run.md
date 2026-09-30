# MVP assessment from a real end-to-end run (2026-09-29)

- **Author/agent:** implementer agent
- **Scope:** the whole pipeline, driven from an empty catalog through every stage, with logging on
- **Verdict:** **MVP is real and the two features that make it a product — content-addressed builds and
  lineage — work correctly. It is not production-ready, and the run is the evidence for why.**

This is an assessment of what happened, not of what the code says happens. Every number below was
read back out of the running system after it was reset, and five of the findings are defects that
had shipped and that neither the test suite nor reading the source had caught.

## What was run

Database truncated to 12 empty tables, artifact blobs and the metrics log deleted, server restarted.
First-run state confirmed: the ingest panel appears, `GET /api/v1/jobs` and `/episodes` both empty.

| Step | How | Result |
|---|---|---|
| Ingest MCAP bag (20.1 MiB, 10 min, 72 600 messages, 16 MiB attachment) | `POST /ui/jobs` form | succeeded, 2.65 s |
| Ingest LeRobot v3 (`lerobot/svla_so101_pickplace`) | same form | succeeded, 0.26 s |
| Ingest LeRobot v2 (`lerobot-driving-school`) | same form | succeeded, 0.21 s |
| Ingest synthetic episode | same form | succeeded, 0.14 s |
| Validate all 5 against a 7-rule profile | `POST /api/v1/jobs` | 4 passed, 1 quarantined, 0.73 s |
| Build over the selection | `POST /api/v1/jobs` | 4 episodes, `bld_0daa22c8…` |
| Rebuild the same selection | same | **same address, no second build row** |
| Build under a different policy | same | **different address** |
| Reverse lineage, both directions | `GET /api/v1/episodes/{id}/builds` | correct |
| 9 UI pages | `GET /ui/*` | all 200, zero `/api/v1` strings in rendered text |

**Five input formats and paths, one pipeline, no step skipped and no step mocked.** The synthetic
episode was quarantined for `FRAME_RATE_UNDECLARED` — the rule working, not a failure.

Observability held up under the run: 3 177 metric records across 19 distinct names in
`var/metrics/runtime.jsonl`, zero tracebacks, zero 5xx. Correlation ids carried from the form POST
through to the job row. The monitoring tick ran in 0.39 s and correctly reported itself not blind.

## The five defects

These are the most valuable output of the run, and every one has a shape worth recording.

### 1. A build shipped a quarantined episode (high)

`BuildPayload`'s docstring says: *"Empty `episode_ids` means 'every episode that passed validation' —
the selection a first build almost always wants, and the one that is safe to default to **because a
failing episode is excluded rather than silently included**."*

Validate and build shared one `_selection` that returned `list_episodes()` — every episode, no state
filter. The first build contained all four episodes, including the one the validator had just
rejected, in the same job, minutes apart. The promise was in the API's own documentation and had no
code behind it.

### 2. The build did not pin its policy (medium-high)

The manifest cites the validation profile as `{hash, name, version}`. The `hash` was **always the
empty string**, and `GET /api/v1/builds` returned `profile_hash: null` for every build.

Cause: `builds/service.py` read `profile.get("hash")`, but `ValidationProfile.to_dict()` emits no
`hash` key — the field is `content_hash`, and nothing ever set it. `ValidationProfile.content_hash`
was a dead field: declared, defaulted to `""`, never populated, never serialised.

What saved the determinism claim from collapsing entirely is worth noting too: the profile's *name*
also enters the manifest, and `register_validation_profile` refuses a name/version collision with
different content, so two different policies could not silently share an address. But the
content-address field — the thing that makes the citation robust to a policy being edited — was
decorative. An empty address is worse than none, because it looks recorded.

*(An earlier reading of this evidence was wrong and is recorded because it nearly became a false
report: two builds under materially different policies appeared to produce the same address. They did
not. The second "policy" reused an existing name and version, and the system correctly refused it —
`IdempotencyConflict`, a guard working exactly as ADR 0016 intends. Re-running with a distinct name
produced two distinct addresses.)*

### 3. An episode's length was the quality sample's length (high)

`episode_quality.frame_count` is *how many frames the quality analysis saw*. Six read paths used it
as *how long the episode is*: the episodes list, the length distribution, the length z-score, the
`short`/`long` curation ordering, `frame_count_median` in the monitoring snapshot, and the Episodes
page.

For a non-streaming reader the two numbers are identical, so the bug was invisible. The MCAP
reader analyses a decimated window, and the result on the operator's screen was:

```
file=so101_pick_place.mcap   mcap   valid   816 frames   moderate
```

for a log of **72 600 messages**. An 89× under-report, in the column an operator sorts and filters
on, produced by a coincidence that held for every format that existed before a streaming one.

### 4. Reverse lineage returned three permanently empty fields (medium)

`builds_for_episode` selected `hash, name, episode_count, created_at` against a seven-field response
schema. `profile_hash`, `code_commit` and `job_id` came back as their schema defaults — no error, no
warning, just `null` on every call. The endpoint answers "which builds contain this episode, under
what policy, from what commit?" and was answering two of the three questions with `null`.

### 5. A terminal failure spent three attempts (low)

A profile name/version collision is a property of the payload and fails identically on every attempt,
so it belongs in `_TERMINAL_FAILURES` and did not. It burned the full retry budget, delayed the
operator's signal, and counted three failures in `jobs_failures_total` for one mistake — which was
enough to trip the notifier's `REPEATED_READ_FAILURE` rule on the operator's own bad input. A true
positive, reached for the wrong reason.

## What is genuinely good

Saying this plainly, because the defect list above is not a verdict on the system:

- **Determinism is structural, not aspirational.** The rebuild produced the identical `bld_` address
  and did not create a second row, through the live queue and a real database — not just in a unit
  test with a fake catalog. A different policy produced a different address. The product's central
  claim held under the one test that matters.
- **Format extensibility was a real promise and it held.** The MCAP reader required no change to the
  ingest service, validation, the catalog, the API, or the job queue — one entry in a list. That is
  the architecture paying off, and it was only believable once a second format existed.
- **The quality/curation layer is more than a demo.** `smooth`/`moderate`/`jerky` verdicts
  distinguished a 460-frame driving episode (`jerky`) from a 303-frame pick-place episode
  (`moderate`) from a 72 600-message log (`moderate`) with no tuning. The quarantine path fired
  correctly and for the right reason.
- **The notifier found a real fault on its own,** during the run, before anyone looked at the logs.
- **Ingest memory is bounded by design and the reader honours it** — a 20.1 MiB bag analysed through
  a self-halving window rather than materialised.

## What is not good enough

- **Ingest is 8× short of its target and the reason is ours.** 6.21 MiB/s against a provisional
  50 MB/s. The container reads at 92 MiB/s and JSON decode at 58 MiB/s, so neither the format nor
  the disk is the obstacle; one function rebuilding a path string per numeric leaf is 51% of total
  ingest time. See [EXP-0004](../experiments/0004-mcap-ingest-baseline.md).
- **A build is a manifest, not a dataset.** Nothing is materialised as LeRobot files on disk. The
  reproducible thing is the address and the lineage, not bytes a trainer can open.
- **Two of five defects were in the build feature added days ago and covered by 13 tests.** The
  determinism tests exercise `DatasetBuilder`, which is pure and correct. The defects were all in
  the wiring *around* it — selection, serialization, read queries. **The lesson generalises: pure
  core plus unit tests is not coverage of the feature.**
- **Coverage is 92% and the defects were still there.** Line coverage is not a claim about which
  questions were asked.
- **A real ROS 2 bag has not been through this.** The MCAP fixture is JSON-encoded by construction,
  which is the reader's expensive path. CDR and protobuf channels are counted and named but
  contribute no numbers. Whether the JSON path is representative of production ROS 2 logs is unknown
  and is the single largest open question about this ingest path.

## What I would do next, in order

1. **B-019** — compile the MCAP dimension layout once per topic. Measured, scoped, and the largest single win available.
2. **Extend the run into a committed end-to-end test** over HTTP against a live server, so the wiring — not just the core — is gated. Every one of the five defects lived in the wiring.
3. **Measure a real `ros2msg` bag.** It decides whether the 6.21 MiB/s number describes production at all.
4. **Materialise a build as LeRobot v3 files**, so "content-addressed dataset" is a directory a trainer can open rather than a row.
5. **The chaos harness** (B-016), which is the only way to get a detection-accuracy figure and the only way to close the failure-mode catalog.

## Reproducing this

```bash
just setup && just pg-up          # or point DE_DATABASE_URL at a local Postgres
just reset --yes                  # empty catalog, artifacts, metrics
just run                          # first-run ingest panel appears on /ui
```

Then ingest one file of each format through the form at `http://127.0.0.1:8000/ui`, submit a
`validate` job and a `build` job, and read back the addresses. `scripts/make_mcap_log.py` generates
the bag deterministically; `just bench --workload mcap-ingest` measures it.
