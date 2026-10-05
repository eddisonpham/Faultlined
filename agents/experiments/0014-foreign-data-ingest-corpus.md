# EXP-0014 — Foreign-data ingest corpus: what happens to data that is not LeRobot or MCAP-JSON

- **Date:** 2026-10-04
- **Commit:** `bab9ab5` (clean tree)
- **Hardware:** Windows 11, x86_64, PostgreSQL 17.11 on 127.0.0.1:55432
- **Tooling:** `scripts/foreign_data_corpus.py` (generator), `scripts/foreign_data_probe.py` (probe)
- **Method:** [methodology](../benchmarking/methodology.md). Synthetic corpus, explicitly labelled as such (rule 1). One
  probe per fixture through the unmodified reader registry, and through a real `ingest_source` job in a throwaway
  database (rule 7).

## What this measures and why

Every reader in this repository was verified against LeRobot v2.1/v3.0 and against an MCAP file this project's own
generator wrote. Both are a narrow slice of what is on a robot engineer's disk. Nothing in the test suite has ever
offered the engine a **ROS 2 sqlite3 bag**, an **HDF5 UMI recording**, an **RLDS/TFDS dataset**, a **zarr store**, or
a **`.zip` of a bag** - so "my reader works" was never evidence about "my reader works on my data".

Nineteen deterministic fixtures were generated (no RNG, no clock; the same arguments produce byte-identical output),
each imitating a layout an operator actually has. Every one was then pushed through the **unmodified** pipeline:
`read_episode()` from the registry, and a real `ingest_source` job executed by a real `IngestWorker` against a
throwaway database, reading back the terminal job row and the catalog rows it wrote.

The three outcomes that matter are *accepted with correct numbers*, *refused with an honest error*, and **silently
wrong**. The third is the only one that damages a catalog, because everything downstream inherits it.

## Result

**9 of 19 fixtures are accepted. 10 are refused. Two of the nine are accepted without producing the signal they
exist to provide.**

| Fixture | Reader | Job | Frames | Quality verdict | Decoded channels |
|---|---|---|---|---|---|
| `mcap_json` | mcap | succeeded | 2000 | `smooth` | 2 |
| `mcap_flat` | mcap | succeeded | 1000 | `smooth` | 1 |
| `mcap_ragged` | mcap | succeeded | 1000 | `smooth` | 1 |
| **`mcap_cdr`** | mcap | **succeeded** | 1000 | **`None`** | **0** |
| **`mcap_protobuf`** | mcap | **succeeded** | 200 | **`None`** | **0** |
| **`mcap_gripper_topic`** | mcap | **succeeded** | 1000 | **`unknown`** | 1 |
| `mcap_nonfinite` | mcap | **failed (3 attempts)** | - | - | - |
| `mcap_empty` | mcap | **succeeded** | **0** | `None` | 0 |
| `truncated_mcap` | mcap | failed (honest) | - | - | - |
| `lerobot_v4` | lerobot | failed (honest) | - | - | - |
| `ros2_sqlite_bag` | **none** | failed | - | - | - |
| `umi_hdf5` | **none** | failed | - | - | - |
| `rlds_tfds` | **none** | failed | - | - | - |
| `zarr_droid` | **none** | failed | - | - | - |
| `webdataset_tar` | **none** | failed | - | - | - |
| `npz_flat` | **none** | failed | - | - | - |
| `lerobot_no_meta` | **none** | failed | - | - | - |
| `video_only` | **none** | failed | - | - | - |
| `zip_containing_mcap` | **none** | failed | - | - | - |
| `not_a_dataset` | **none** | failed | - | - | - |

Refusals carry the error the design asks for - `no reader recognises <path> (tried: mcap, lerobot)` - except for the
one defect below.

## Three defects found

### D1 (major) - the topic name silently decides the quality verdict

`mcap_json` and `mcap_gripper_topic` contain **byte-identical payloads**. The only difference is the name of the
topic: `/joint_states` versus `/left_gripper/joint_states`.

| Fixture | Busiest multi-dim topic | Verdict |
|---|---|---|
| `mcap_json` | `/joint_states` | `smooth` |
| `mcap_gripper_topic` | `/left_gripper/joint_states` | **`unknown`** |

Mechanism, confirmed by reading the code: `analysis/quality.py` excludes any dimension whose *name* matches
`_GRIPPER = re.compile("grip", re.IGNORECASE)`, and `ingest/readers/mcap_reader.py` builds each dimension name as
`<topic>.<path>`. So the topic name decides which dimensions are allowed to judge motion. `_scorable` picks the
busiest topic with more than one dimension - a gripper joint stream qualifies - every one of its dimensions is
excluded as a "gripper", `judged_dims` becomes 0, and `analyze` returns the documented degenerate verdict `unknown`.

This is not a corner case. Any bimanual or gripper-heavy rig publishing `/left/gripper/joint_states`,
`/robot/gripper_state` or `/right_arm/grip` hits it. The episode **ingests successfully**, lands in the catalog, and
carries no motion verdict, with nothing on any page explaining why.

Second-order effect of the same naming choice: the topic name is baked into the dimension name, so the same physical
quantity is `/joint_states.position[0]` in one bag and `position[0]` in another. Per-dimension statistics are
therefore not comparable across episodes, which is most of what the catalog's quality columns are for.

### D2 (major) - one non-finite value takes the whole episode down, three times over

`mcap_nonfinite` carries a single `NaN` at t=5 s and a single `Infinity` at t=15 s in an otherwise ordinary bag.
The job outcome:

```
('queued', attempts=1) -> ('queued', attempts=2) -> ('failed', attempts=3)
error: {'type': 'InvalidTextRepresentation', 'message': 'job handler failed'}
```

Root cause, from the traceback: the reader's per-channel accumulator takes `max = Infinity` without a finite guard,
and `INSERT INTO episodes ... Jsonb(metadata)` rejects `Infinity` - PostgreSQL has no JSON representation for it.
ADR 0023 states that "a non-finite value now stops the analysis, reports how many were seen, and scores zero". That
guarantee holds inside `analyze()`; it **does not hold for `ChannelStats`**, which is computed separately in the
reader. So the documented protection is real and incomplete at the same time.

Compounding it: `InvalidTextRepresentation` is classified **retryable**, so the same bytes are re-read from disk
three times before the job parks. Retrying cannot help - the answer is a property of the file.

### D3 (major) - the operator is told "job handler failed"

Every failed job in the run stores, verbatim:

```python
{"type": "InvalidTextRepresentation", "message": "job handler failed"}
```

`jobs/worker.py:482` writes the literal string `"job handler failed"` as the message for every handler exception and
logs the real one. The specific, actionable text - `no reader recognises var/.../umi_hdf5 (tried: mcap, lerobot)` -
is never persisted, so it never reaches `GET /api/v1/jobs/{id}`, the Jobs page, or the Failures page. For a data
engineering tool whose stated value is failing honestly, the single question an operator has - *why was my data
rejected?* - is answered with a constant.

## Two honest gaps, correctly refused

- **`ros2_sqlite_bag`** - the **default `ros2 bag record` output**. The most common file on a ROS 2 robot's disk has
  no reader. The engine's own docs call MCAP "the natural input for the raw end of the pipeline"; MCAP is what
  `ros2 bag convert` writes, not what `ros2 bag record` writes.
- **`zip_containing_mcap`** — a `.zip` containing a perfectly readable bag, which is how recordings arrive when
  someone sends you a drive. Refused.

Also absent, and expected to stay absent for now: no reader handles **video**. `video_only` is refused, which is the
right behaviour for a catalog that stores metadata rather than frames, but it means the engine cannot describe the
data that actually occupies the bytes on a modern robot.

## What the corpus cannot tell us

- Fixtures are synthetic. `ros2_sqlite_bag`, `umi_hdf5` and `rlds_tfds` reproduce the *shape* of those layouts, not
  the variety inside them. A real HDF5 file has ragged groups, vlen strings, and compound dtypes this generator does
  not produce.
- `not_a_dataset` and the ten refusals are confirmed at the reader boundary and at the job boundary, but no operator
  was asked to recover from them; the error *text* was only assessed by reading it.
- No fixture exercises a genuinely large foreign file, so no foreign-format throughput number exists. The MCAP
  scaling curve in EXP-0010a does not transfer to a format with no reader.

## Reproduction

```bash
uv run --with h5py python scripts/foreign_data_corpus.py var/foreign-corpus
uv run python scripts/foreign_data_probe.py var/foreign-corpus --json var/foreign-probe.json
uv run python scripts/foreign_data_probe.py var/foreign-corpus --in-process --json var/foreign-probe-jobs.json
```

`--with h5py` is needed only by the generator, and only for the UMI fixture; the engine itself does not import it.

## Conclusion

The format boundary is honest but narrow. Of the eleven layouts an operator is most likely to arrive with, the engine
reads two. The refusals are clean, which is the design working as intended - but "clean refusal" is not "support",
and one of the two supported encodings (CDR, which is what `ros2 bag` writes) produces an episode with **no
decoded channels and no quality verdict at all**, which is closer to the "silently wrong" outcome than to support.

Three defects are recorded and unfixed as of this record: D1 (topic name decides the verdict), D2 (non-finite
channel statistic fails the whole episode, retried three times), D3 (failure reason not persisted). See the
assessment in [the 2026-10-04 review](../reviews/2026-10-04-engineer-assessment-verdict.md).