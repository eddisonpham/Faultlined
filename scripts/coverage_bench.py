"""Measure what the coverage report costs at scale (ADR 0033).

The claim under test is not "coverage is useful" - that is a judgement - but the two things that
would make it *wrong to ship*: that the report is bounded by the axes rather than by the episode
count, and that it is cheap enough to sit in the build page.

**The falsifier, stated before anything is run.** The idea is dead if, at 10 000 episodes:

- the payload grows with the episode count rather than staying flat at the axis caps, or
- the whole read plus fold exceeds 200 ms (the page already pays ~224 ms for the redundancy
  report at 96 episodes per EXP-0020, and this surface exists to be cheaper than that), or
- the gap list is empty or trivially "everything is covered" for a build that visibly holds a
  fraction of the catalog.

Nothing is written to the repository. The numbers become EXP-0021. It needs a throwaway database
on the isolated cluster and refuses one that already holds episodes, because it measures absolute
state (a shared catalog would make the report describe someone else's episodes):

    export DE_DATABASE_URL=postgresql://data_engine@127.0.0.1:55432/bb_coverage
    uv run --all-extras python scripts/coverage_bench.py --episodes 100 1000 10000

The corpus plants a vocabulary of its own - one entry per task string, plus a handful of labels
with no episode anywhere - so both reference sets are exercised: the catalog's differences and the
operator's unfilled tasks. One in ten episodes is left unscored on purpose, so the verdict axis
carries its own `(unscored)` value rather than a tidy sweep.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any

from psycopg.types.json import Jsonb

from data_engine.analysis.coverage import GAP_LIMIT, VALUE_LIMIT, CoverageReport, coverage_report
from data_engine.builds.service import DatasetBuilder
from data_engine.catalog import vocabulary
from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings, load_settings

VERDICTS = ("smooth", "jerky", "stalled")
FORMATS = ("synthetic-json", "lerobot-v3", "mcap")
ROBOTS = ("so101", "ur5e", "franka-panda", "abb-irb-1200")
COST_BUDGET_MS = 200.0


def _seed(
    catalog: PostgresCatalog, settings: Settings, episodes: int, *, tasks: int, unfilled: int
) -> list[str]:
    """A catalog with a populated vocabulary and `episodes` episodes spread across the axes.

    The episodes are written with the writer's own statements on **one** connection rather than
    through `register_episode` per episode. That is a deliberate exception to "seed the real way",
    and it is here because of a number this bench produced: a repository call opens its own
    connection, and one connection on this host costs ~60 ms, so seeding 10 000 episodes the
    per-row way is thirty minutes of `connect()` and measures Windows, not the report. The read
    path - the catalog query and the fold - is untouched and is what all the numbers below are
    about. The vocabulary does go through `vocabulary.create_entry`/`map_task`, because ADR 0029's
    bookkeeping (mappings plus events) is not something a bench should re-implement.
    """
    if catalog.count_episodes():
        raise SystemExit(
            f"refusing to run: the catalog already holds {catalog.count_episodes()} episodes. "
            "This bench counts absolute state; point it at a fresh database (see the docstring)."
        )
    for index in range(tasks):
        label = f"task {index:03d}"
        entry = vocabulary.create_entry(settings, preferred_label=label)
        vocabulary.map_task(settings, task_string=f"raw/{label}", entry_id=entry["id"])
    for index in range(unfilled):
        vocabulary.create_entry(settings, preferred_label=f"unfilled task {index:02d}")

    job, _ = catalog.submit_job("ingest", {}, "coverage-bench", "coverage-bench")
    job_id = str(job["id"])
    ids = [f"covbench-{index:06d}" for index in range(episodes)]
    artifacts = [(f"covbench-{index:06d}", 256) for index in range(episodes)]
    episode_rows = [
        (
            episode_id,
            episode_id,
            episode_id,
            episode_id,
            FORMATS[index % len(FORMATS)],
            Jsonb({"task": f"raw/task {index % tasks:03d}", "robot": ROBOTS[index % len(ROBOTS)]}),
        )
        for index, episode_id in enumerate(ids)
    ]
    lineage = [("episode", episode_id, "job", job_id, "produced_by") for episode_id in ids]
    quality = [
        (
            episode_id,
            90,
            0.01,
            0.01,
            0.0,
            VERDICTS[index % len(VERDICTS)],
            Jsonb([]),
            0,
            None,
            0.0,
            "unknown",
            "unknown",
            None,
            0,
            Jsonb([]),
        )
        for index, episode_id in enumerate(ids)
        if index % 10  # one in ten left unscored, so the axis carries `(unscored)`
    ]
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO artifacts (hash, size_bytes) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            artifacts,
        )
        cursor.executemany(
            "INSERT INTO episodes (id, source_hash, episode_key, artifact_hash, format, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            episode_rows,
        )
        cursor.executemany(
            "INSERT INTO lineage_edges (from_type, from_ref, to_type, to_ref, relation)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            lineage,
        )
        cursor.executemany(
            "INSERT INTO episode_quality (episode_id, frame_count, movement_score, jerk_score,"
            " stall_ratio, verdict, dims, nonfinite, max_gap_seconds, gap_ratio, integrity,"
            " worst_verdict, worst_dim, judged_dims, motion_trace)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            quality,
        )
    return ids


def _build(catalog: PostgresCatalog, episode_ids: list[str], name: str) -> str:
    result = DatasetBuilder(code_commit="coverage-bench").build(
        catalog.get_episodes(episode_ids), name=name
    )
    catalog.record_build(result, job_id="job-coverage-bench")
    return result.hash


def connection_floor(catalog: PostgresCatalog, *, repeats: int = 9) -> float:
    """Median milliseconds of one repository call, measured in this process.

    Every repository call opens its own connection, and on this host that is ~60 ms, so the raw
    read number is mostly `connect()`. Measuring the floor beside it is the difference between
    "the coverage query is slow" and "every route on this host pays this", and only one of those
    is worth acting on.
    """
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        catalog.count_episodes()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def report_cost(catalog: PostgresCatalog, build_hash: str, *, repeats: int = 5) -> dict[str, Any]:
    """Time the real path - the catalog query, then the pure fold - and measure the payload."""
    read: list[float] = []
    fold: list[float] = []
    report: CoverageReport | None = None
    inputs: dict[str, Any] = {}
    payload = 0
    for _ in range(repeats):
        start = time.perf_counter()
        inputs = catalog.build_coverage_inputs(build_hash)
        read.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        report = coverage_report(build_hash, inputs)
        fold.append((time.perf_counter() - start) * 1000)
        # The wire payload, not the object: this is what the page and the JSON route ship.
        payload = len(json.dumps(report.to_dict(), separators=(",", ":")).encode("utf-8"))
    assert report is not None
    task = report.axis("task")
    assert task is not None
    return {
        "read_p50_ms": statistics.median(read),
        "read_max_ms": max(read),
        "fold_p50_ms": statistics.median(fold),
        "total_p50_ms": statistics.median(read) + statistics.median(fold),
        "payload_bytes": payload,
        "rows": len(inputs["values"]),
        "episodes": report.episode_count,
        "catalog": report.catalog_size,
        "coverage_ratio": report.coverage_ratio,
        "task_values": len(task.values),
        "task_gaps_shown": len(task.gaps),
        "task_gaps_total": task.missing,
        "truncated_axes": sum(1 for axis in report.axes if axis.truncated),
    }


def _print_row(label: str, row: dict[str, Any], *, floor_ms: float) -> None:
    # The read opens two connections (values, then the vocabulary); the floor is what that costs.
    aggregation = row["read_p50_ms"] - 2 * floor_ms
    print(
        f"  {label:>10s}  {row['read_p50_ms']:>8.1f}  {row['read_max_ms']:>8.1f}  "
        f"{aggregation:>7.1f}  {row['fold_p50_ms']:>7.2f}  {row['total_p50_ms']:>9.1f}  "
        f"{row['rows']:>5d}  {row['payload_bytes']:>8d}  {row['task_values']:>5d}  "
        f"{row['task_gaps_shown']:>4d}/{row['task_gaps_total']:<4d}  {row['truncated_axes']:>4d}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, nargs="*", default=[100, 1000, 10000])
    parser.add_argument("--tasks", type=int, default=200, help="distinct task strings to plant")
    parser.add_argument("--unfilled", type=int, default=12, help="labels with no episode at all")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    sizes = sorted(set(args.episodes))
    print("coverage report · ADR 0033 · falsifier: payload must stay at the axis caps,")
    print(
        f"  read+fold must stay under {COST_BUDGET_MS:.0f} ms at the largest size, "
        "and the gap list must not be empty"
    )
    print(
        f"  caps: {VALUE_LIMIT} values and {GAP_LIMIT} gaps per axis · "
        f"corpus: {args.tasks} task labels + {args.unfilled} unfilled"
    )

    settings = load_settings()
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)
    seeded = time.perf_counter()
    ids = _seed(catalog, settings, max(sizes), tasks=args.tasks, unfilled=args.unfilled)
    print(
        f"  seeded {len(ids)} episodes and {args.tasks + args.unfilled} vocabulary entries "
        f"in {time.perf_counter() - seeded:.1f}s"
    )

    # Warm-up, so the first measured read is not paying for a cold connection pool.
    whole_hash = _build(catalog, ids, "coverage-bench-all")
    catalog.build_coverage_inputs(whole_hash)
    floor_ms = connection_floor(catalog)
    print(f"  one repository call (its own connection) costs {floor_ms:.1f} ms on this host")

    print()
    print(
        "    episodes  read p50  read max  agg p50  fold p50  total p50   rows   payload  "
        "tasks  gaps shown/total  trunc"
    )
    print(
        "    (agg p50 is the read minus two connection floors: what the aggregation itself cost."
        " A negative value means it fitted inside the setup jitter, not that it was free.)"
    )
    measured: dict[int, dict[str, Any]] = {}
    for size in sizes:
        build_hash = whole_hash if size == len(ids) else _build(catalog, ids[:size], f"c-{size}")
        measured[size] = report_cost(catalog, build_hash, repeats=args.repeats)
        _print_row(str(size), measured[size], floor_ms=floor_ms)

    whole = report_cost(catalog, whole_hash, repeats=args.repeats)
    print()
    _print_row("all", whole, floor_ms=floor_ms)
    print(
        f"  the whole-catalog build holds {whole['episodes']} of {whole['catalog']} episodes "
        f"({whole['coverage_ratio'] * 100:.0f}%) and leaves "
        f"{whole['task_gaps_total']} named task(s) unfilled"
    )

    # Judged on the largest size asked for and not on the run that happens to be last: the
    # whole-catalog build is the same read as the largest size, and measuring it twice is how a
    # budget gets passed by accident.
    largest = measured[max(sizes)]
    smallest = measured[min(sizes)]
    growth = largest["payload_bytes"] / max(smallest["payload_bytes"], 1)
    payload_ok = growth < 1.25
    gaps_ok = largest["task_gaps_total"] > 0
    cost_ok = largest["total_p50_ms"] < COST_BUDGET_MS
    print(
        f"  falsifier at {max(sizes)} episodes: payload bounded "
        f"({'MET' if payload_ok else 'BROKEN'}, {smallest['payload_bytes']} -> "
        f"{largest['payload_bytes']} B = {growth:.2f}x) · gaps non-empty "
        f"({'MET' if gaps_ok else 'BROKEN'}, {largest['task_gaps_total']}) · "
        f"read+fold under {COST_BUDGET_MS:.0f} ms "
        f"({'MET' if cost_ok else 'BROKEN'}, {largest['total_p50_ms']:.1f} ms)"
    )
    if not cost_ok:
        print(
            f"  the overrun is not the aggregation: "
            f"{largest['read_p50_ms'] - 2 * floor_ms:.1f} ms of query and "
            f"{largest['fold_p50_ms']:.2f} ms of fold against {2 * floor_ms:.1f} ms of setup"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
