# ADR 0025: LeRobot v3 export of dataset builds

- **Date:** 2026-09-30
- **Status:** accepted
- **Stage:** 4 (client-usable iteration)
- **Related:** [ADR 0006](0006-storage-and-formats.md) (LeRobot v3 is an accepted output contract),
  [ADR 0022](0022-mcap-ingest-reader.md) (reader registry), FR-006, FR-007, NFR-004,
  [definition-of-done §2](../spec/definition-of-done.md) ("Still open: exporting the build as a
  LeRobot v3 dataset on disk")

## Context

The stage-4 goal is a local client that *uses* the engine in its working loop: ingest,
validate, curate, build, then hand the result to a training script. Everything before the
last arrow works. A build today is a manifest plus lineage rows: `GET /api/v1/builds/{hash}`
answers what went into a build and why, and nothing answers "give me the files". The
definition of done has carried this as the one open MVP item since 2026-09-29.

The owner picked this feature over a load campaign and a workload registry (run records)
for stage 4, with load testing (NFR-003, item 5 of the stage plan) following immediately.

## Decision

1. **Export is a worker job (`JobType.EXPORT`, payload `{build_hash, name?}`), not an API
   side effect.** It can be queued, retried, cancelled, observed, and survive restarts
   like every other stage; a synchronous "build → write a tree" endpoint would be the one
   unkillable request in the system and the retry policy would not apply to it.

2. **The export root is `var/exports/{build_hash}/` (configurable via `DE_EXPORT_ROOT`),
   written atomically per dataset.** The build hash is the directory name because the
   hash *is* the dataset's identity (ADR's build determinism rule). The tree is written
   to a `.pending-` sibling and renamed into place on success, so a killed export never
   leaves a half-valid dataset at the address. Re-export of an existing verified tree is
   a no-op that returns the same path: the content address makes "already done" a
   verifiable claim, not an assumption.

3. **One data file per source episode, episodes in manifest (sorted) order.** A v3 dataset
   is a row-range contract over concatenated files; a per-episode file is the simplest
   layout that satisfies it, and it keeps each file verifiable against the source
   episode's own `artifact_hash` lineage. Chunk boundaries follow the format's 1000-file
   convention if a build ever exceeds it.

4. **Feature columns come from the episodes' stored channel names; `timestamp` is
   written from the stored frame series.** The engine's catalog is the source of truth
   for what an episode contains; the export re-serialises from artifact bytes and
   catalog metadata rather than inventing schema. Episodes whose artifact cannot be
   decoded into frames fail the export with a reason code rather than silently shipping
   a dataset missing an episode - the build's episode count is a promise.

5. **The exported tree carries a `_faultlined/manifest.json` alongside the LeRobot v3
   files**, embedding the build manifest's identity (hash, episodes + source hashes,
   profile hash, code commit). LeRobot consumers ignore the extra directory; lineage
   travels with the data, which is the whole point of the format choice.

6. **The strictest consumer of the export is our own LeRobot v3 reader.** The acceptance
   test is a round trip: export a build, point `LeRobotReader` at the result, read every
   episode back, and require frame counts, task, and robot type to match the catalog
   rows. Any export our own reader rejects is a defective export by definition.

## Consequences

- **Synthetic episodes export as real LeRobot data.** Their canonical-JSON artifacts are
  decoded to frames (the ingest service defines the column naming: `observation.state` ->
  `observation[i]` series) and re-serialised to Parquet. A client can ingest synthetic
  episodes, curate, and train - the loop closes on the smallest input we support.
- **LeRobot-sourced episodes export by re-serialising the slice**, not by copying the
  source file (a v3 source file holds many episodes; copying it would duplicate other
  builds' episodes and break the row-range contract).
- **Videos are not exported.** No ingested format carries video frames today
  (`has_video` is recorded, frames are not); an exported dataset declares
  `total_videos: 0`. This is a recorded limit, not an omission.
- **Only Parquet-backed episodes can be exported (added 2026-10-06).**
  `_parquet_frames` reads each episode's artifact as Parquet, so a build
  containing an episode ingested from MCAP fails export: the handler raises a
  terminal `ExportError` naming the episode and the reader's own complaint
  (`Parquet magic bytes not found in footer`). Such episodes are readable,
  validatable, buildable and queryable - only export is out of reach, until an
  MCAP-to-columnar converter exists. This decision was written against the
  LeRobot v3 target and the synthetic source and never stated the boundary; it
  is recorded here because a user otherwise meets it as a failed job.
- **The build hash does not change.** Export is a projection of an immutable manifest;
  nothing about the build is mutated. `builds.exported_at` is not introduced - the
  filesystem address and the artifact store are the record.
- **Jobs: `export` joins `build` as a selection-shaped handler.** Terminal failures
  (unknown build hash) skip retry; transient I/O failures retry.
- **Deferred:** resumable/chunked export of very large builds, video shard remuxing,
  upload to external object storage (deferred table in requirements; trigger unchanged).

## Alternatives considered

- **API-synchronous export** (`POST /builds/{hash}/export` writes the tree inline):
  simplest, and wrong - a 10 GB build is a 10 GB request body's worth of I/O inside one
  HTTP handler, uncancellable and unretriable. The job queue exists; use it.
- **Copy-on-build** (materialise the dataset at build time): couples two stages, makes
  the cheap common case (manifest-only build for lineage queries) pay the expensive
  case, and writes data for builds nobody trains on.
- **Single concatenated data file** (fewer files, closer to Hub layout): requires a
  second pass to compute row ranges and makes per-episode integrity checks harder; the
  per-episode layout is smaller to reason about and still satisfies the v3 contract.
- **Engine-native format instead of LeRobot v3**: rejected; ADR 0006 already committed
  to the ecosystem contract, and the whole value is that the output is loadable by
  tooling we do not have to write.
