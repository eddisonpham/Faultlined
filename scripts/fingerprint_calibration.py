"""Measure what the behavioural fingerprint can actually see (ADR 0032)."""

from __future__ import annotations

import argparse
import math
import random
import statistics
import time
from typing import Any

from data_engine.analysis.fingerprint import (
    COMPONENT_WEIGHTS,
    DEFAULT_THRESHOLD,
    MAX_EPISODES,
    redundancy_report,
)
from data_engine.analysis.quality import analyze

SEED = 20261007
FRAMES = 90
DIMS = 4


def _behaviour(index: int) -> dict[str, list[float]]:
    """One 'behaviour': a multijoint motion at its own speed."""
    period = 4.0 + index * 0.75
    weight = [0.35 + 0.08 * index + 0.05 * joint for joint in range(DIMS)]
    return {
        f"position[{joint}]": [
            weight[joint] * math.sin(2 * math.pi * frame / (period + joint * 0.5))
            for frame in range(FRAMES)
        ]
        for joint in range(DIMS)
    }


def _record(
    rng: random.Random, series: dict[str, list[float]], *, jitter: float = 0.02
) -> dict[str, list[float]]:
    """A second take of the same behaviour: amplitude jitter, a little noise, a time warp."""
    amplitude = 1.0 + rng.uniform(-jitter, jitter)
    warp = 1.0 + rng.uniform(-jitter, jitter)
    out: dict[str, list[float]] = {}
    for name, values in series.items():
        progressed = [
            values[min(int(frame * warp), len(values) - 1)] for frame in range(len(values))
        ]
        out[name] = [
            value * amplitude + rng.uniform(-jitter, jitter) * 0.05 * (abs(value) + 1e-6)
            for value in progressed
        ]
    return out


def _decoy(rng: random.Random, index: int) -> dict[str, list[float]]:
    """A hard negative: the same joints at the same speed, moving out of step."""
    period = 6.0 + (index % 7) * 2.5
    weight = [0.4 + ((index + joint) % 5) * 0.15 for joint in range(DIMS)]
    phase = [rng.uniform(0.0, 2 * math.pi) for _ in range(DIMS)]
    return {
        f"position[{joint}]": [
            weight[joint] * math.sin(2 * math.pi * frame / (period + joint * 0.5) + phase[joint])
            for frame in range(FRAMES)
        ]
        for joint in range(DIMS)
    }


def _row(episode_id: str, series: dict[str, list[float]], *, behaviour: int) -> dict[str, Any]:
    frames = len(next(iter(series.values())))
    quality = analyze(series, timestamps=[frame * 0.05 for frame in range(frames)]).to_dict()
    return {
        "id": episode_id,
        "task": f"behaviour {behaviour}",
        "verdict": quality["verdict"],
        "judged_dims": quality["judged_dims"],
        "stall_ratio": quality["stall_ratio"],
        "gap_ratio": quality["gap_ratio"],
        "dims": quality["dims"],
        "motion_trace": quality["motion_trace"],
    }


def build_corpus(
    behaviours: int, copies: int, decoys: int
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Planted rows, the group each one truly belongs to, and what kind of row it is."""
    rng = random.Random(SEED)
    rows: list[dict[str, Any]] = []
    truth: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for behaviour in range(behaviours):
        series = _behaviour(behaviour)
        group = f"behaviour-{behaviour}"
        for copy in range(copies):
            episode_id = f"b{behaviour}-c{copy}"
            rows.append(
                _row(episode_id, series if copy == 0 else _record(rng, series), behaviour=behaviour)
            )
            truth[episode_id] = group
            kinds[episode_id] = "recording"
        for index in range(decoys):
            episode_id = f"b{behaviour}-d{index}"
            rows.append(_row(episode_id, _decoy(rng, behaviour), behaviour=behaviour))
            truth[episode_id] = f"{group}-decoy-{index}"
            kinds[episode_id] = "decoy"
    return rows, truth, kinds


def _pairs(values: list[str]) -> list[tuple[str, int, int]]:
    return [(values[i], i, j) for i in range(len(values)) for j in range(i + 1, len(values))]


def score(
    rows: list[dict[str, Any]],
    truth: dict[str, str],
    kinds: dict[str, str],
    threshold: float,
) -> dict[str, float]:
    """Pairwise precision/recall of the report's partition, split by what kind of pair it is."""
    report = redundancy_report(rows, threshold=threshold)
    group_of: dict[str, str] = {}
    for group in report.groups:
        group_of[group.representative] = group.representative
        for item in group.duplicates:
            group_of[item.episode_id] = group.representative

    ids = [str(row["id"]) for row in rows]
    counts: dict[str, list[int]] = {"recording": [0, 0], "decoy": [0, 0], "foreign": [0, 0]}
    for _, i, j in _pairs(ids):
        left, right = ids[i], ids[j]
        if truth[left] == truth[right]:
            kind = "recording" if kinds[left] == kinds[right] == "recording" else "decoy"
        else:
            kind = "foreign"
        collapsed = group_of.get(left) is not None and group_of.get(left) == group_of.get(right)
        counts[kind][1] += 1
        counts[kind][0] += int(collapsed)

    collapsed_all = sum(value[0] for value in counts.values())
    true_positive = counts["recording"][0]
    precision = true_positive / collapsed_all if collapsed_all else 0.0
    recall = true_positive / counts["recording"][1] if counts["recording"][1] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    truth_groups = len(set(truth.values()))
    return {
        "threshold": threshold,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "base_rate": counts["recording"][1] / max(len(_pairs(ids)), 1),
        "episodes": len(rows),
        "distinct_reported": report.distinct_count,
        "distinct_actual": float(truth_groups),
        "recordings_found": float(counts["recording"][0]),
        "recordings_total": float(counts["recording"][1]),
        "decoy_merged": float(counts["decoy"][0]),
        "decoy_pairs": float(counts["decoy"][1]),
        "foreign_merged": float(counts["foreign"][0]),
        "foreign_pairs": float(counts["foreign"][1]),
    }


def accuracy_report(behaviours: int, copies: int, decoys: int) -> list[dict[str, float]]:
    rows, truth, kinds = build_corpus(behaviours, copies, decoys)
    thresholds = [0.01, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.16, 0.20]
    return [score(rows, truth, kinds, threshold) for threshold in thresholds]


def _seed_catalog(episodes: int, *, copies: int, decoys: int) -> list[str]:
    """Seed a throwaway catalog through the real writer, and return the episode ids."""
    from data_engine.catalog.database import initialize_schema
    from data_engine.catalog.repository import PostgresCatalog
    from data_engine.config import load_settings

    settings = load_settings()
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)
    behaviours = max(1, math.ceil(episodes / (copies + decoys)))
    rows = build_corpus(behaviours, copies, decoys)[0]
    selected = rows[:episodes] if episodes <= len(rows) else rows
    ids: list[str] = []
    for index, row in enumerate(selected):
        token = f"fingerprint-{index:06d}"
        job, _ = catalog.submit_job("ingest", {}, token, "fingerprint-calibration")
        episode = catalog.register_episode(
            source_hash=token,
            artifact_hash=token,
            size_bytes=256,
            metadata={"task": row["task"], "robot": "so101"},
            job_id=str(job["id"]),
            episode_key=token,
            episode_format="synthetic-json",
        )
        catalog.record_episode_quality(
            str(episode["id"]),
            {
                "frame_count": FRAMES,
                "movement_score": 0.01,
                "jerk_score": 0.01,
                "stall_ratio": row["stall_ratio"],
                "verdict": row["verdict"],
                "dims": row["dims"],
                "gap_ratio": row["gap_ratio"],
                "judged_dims": row["judged_dims"],
                "motion_trace": row["motion_trace"],
            },
        )
        ids.append(str(episode["id"]))
    return ids


def latency_report(sizes: list[int], *, copies: int, decoys: int) -> list[dict[str, float]]:
    """Time the real read + report: one catalog query, then the pure scoring."""
    from data_engine.catalog.repository import PostgresCatalog
    from data_engine.config import load_settings

    catalog = PostgresCatalog(load_settings())
    ids = _seed_catalog(max(sizes), copies=copies, decoys=decoys)
    results: list[dict[str, float]] = []
    for size in sizes:
        window = ids[: min(size, MAX_EPISODES)]
        read_times: list[float] = []
        report_times: list[float] = []
        rows: list[dict[str, Any]] = []
        for _ in range(5):
            start = time.perf_counter()
            rows = catalog.episode_fingerprint_inputs(window)
            read_times.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            report = redundancy_report(rows)
            report_times.append((time.perf_counter() - start) * 1000)
        results.append(
            {
                "episodes": float(len(rows)),
                "read_p50_ms": statistics.median(read_times),
                "read_p95_ms": max(read_times),
                "score_p50_ms": statistics.median(report_times),
                "score_p95_ms": max(report_times),
                "total_p50_ms": statistics.median(read_times) + statistics.median(report_times),
                "redundant": float(report.redundant_count),
                "distinct": float(report.distinct_count),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--behaviours", type=int, default=40, help="distinct motions to plant")
    parser.add_argument("--copies", type=int, default=3, help="recordings of each motion")
    parser.add_argument("--decoys", type=int, default=2, help="hard negatives per motion")
    parser.add_argument(
        "--latency-episodes",
        type=int,
        nargs="*",
        default=[],
        help="sizes to time against a real catalog",
    )
    args = parser.parse_args()

    print(f"fingerprint calibration · seed {SEED} · default threshold {DEFAULT_THRESHOLD}")
    print(f"components: {COMPONENT_WEIGHTS}")
    print(f"corpus: {args.behaviours} motions x ({args.copies} copies + {args.decoys} decoys)")
    print()
    print(
        "  threshold  precision  recall     f1 | recordings caught | foreign merged | decoys merged"
    )
    for row in accuracy_report(args.behaviours, args.copies, args.decoys):
        caught = f"{row['recordings_found']:.0f}/{row['recordings_total']:.0f}"
        foreign = f"{row['foreign_merged']:.0f}/{row['foreign_pairs']:.0f}"
        decoy = f"{row['decoy_merged']:.0f}/{row['decoy_pairs']:.0f}"
        print(
            f"  {row['threshold']:>9.2f}  {row['precision']:>9.3f} {row['recall']:>7.3f} "
            f"{row['f1']:>7.3f} | {caught:>17s} | {foreign:>14s} | {decoy:>13s}"
        )
    if args.latency_episodes:
        print()
        print("  episodes   read p50   read p95  score p50  score p95  total p50  distinct")
        for row in latency_report(args.latency_episodes, copies=args.copies, decoys=args.decoys):
            print(
                f"  {row['episodes']:>8.0f}  {row['read_p50_ms']:>8.1f}  "
                f"{row['read_p95_ms']:>8.1f}  {row['score_p50_ms']:>8.1f}  "
                f"{row['score_p95_ms']:>8.1f}  {row['total_p50_ms']:>8.1f}  "
                f"{row['distinct']:>8.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
