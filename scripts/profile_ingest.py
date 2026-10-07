#!/usr/bin/env python3
"""Attribute MCAP ingest time per stage and dump cProfile hotspots."""

from __future__ import annotations

import argparse
import cProfile
import io
import itertools
import json
import pstats
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from mcap.reader import make_reader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from benchmarks.provenance import collect_provenance  # noqa: E402

from data_engine.ingest.readers.mcap_reader import McapReader  # noqa: E402
from data_engine.ingest.service import EpisodeIngestService  # noqa: E402
from data_engine.storage.artifacts import FileArtifactStore  # noqa: E402

DEFAULT_FIXTURE = ROOT / "var" / "real-data" / "so101_pick_place.mcap"
PROFILE_DIR = ROOT / "var" / "profile"


class _ProfileCatalog:
    """The same in-memory catalog seam the benchmark harness uses."""

    def __init__(self) -> None:
        self.count = 0

    def register_episode(self, **_kwargs: object) -> dict[str, str]:
        self.count += 1
        return {"id": f"episode-{self.count}"}

    def record_episode_quality(self, episode_id: str, quality: dict[str, object]) -> dict[str, str]:
        return {"episode_id": episode_id, "verdict": str(quality["verdict"])}


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def stage_container_only(path: Path) -> float:
    """Read the container and count messages; no payload work at all."""
    start = time.perf_counter()
    count = 0
    with path.open("rb") as stream:
        for _schema, _channel, _message in make_reader(stream).iter_messages(log_time_order=False):
            count += 1
    if not count:
        raise SystemExit(f"{path} yielded no messages; is this the right fixture?")
    return time.perf_counter() - start


def stage_container_and_decode(path: Path) -> float:
    """Container iteration plus JSON decode of every payload - the format floor."""
    start = time.perf_counter()
    count = 0
    with path.open("rb") as stream:
        for _schema, _channel, message in make_reader(stream).iter_messages(log_time_order=False):
            if message.data[:1] in (b"{", b"["):
                json.loads(message.data)
            count += 1
    if not count:
        raise SystemExit(f"{path} yielded no messages")
    return time.perf_counter() - start


def stage_reader(path: Path) -> float:
    """The full reader: decode, flatten, statistics, quality window."""
    start = time.perf_counter()
    extraction = McapReader().read(path)
    if extraction.frame_count <= 0:
        raise SystemExit("reader produced no frames")
    return time.perf_counter() - start


def stage_full_ingest(path: Path, artifact_root: Path) -> float:
    """Sniff, read, describe, hash, and publish - the benchmark's operation."""
    service = EpisodeIngestService(
        _ProfileCatalog(),  # type: ignore[arg-type]
        FileArtifactStore(artifact_root),
    )
    start = time.perf_counter()
    service.ingest_path(path, job_id="profile-ingest")
    return time.perf_counter() - start


def profile_other_workload(workload: str) -> str:
    """cProfile one benchmarked engine stage (one trial, no warmups)."""
    from benchmarks import harness

    operations = {
        "quality-30000f": lambda: harness._quality_benchmark(30_000, trials=1, warmups=0),
        "validation": lambda: harness._validation_benchmark(trials=1, warmups=0),
        "aggregation-500k": lambda: harness._aggregation_benchmark(500_000, trials=1, warmups=0),
        "monitor": lambda: harness._monitor_evaluate_benchmark(trials=1, warmups=0),
        "api-metrics": lambda: harness._api_metrics_benchmark(trials=1, warmups=0),
    }
    operation = operations[workload]
    profiler = cProfile.Profile()
    profiler.enable()
    operation()
    profiler.disable()
    out = io.StringIO()
    stats = pstats.Stats(profiler, stream=out)
    stats.sort_stats("cumulative").print_stats(15)
    return out.getvalue()


def profile_hotspots(path: Path, artifact_root: Path) -> str:
    """cProfile one full ingest; returns the top-functions text table."""
    service = EpisodeIngestService(
        _ProfileCatalog(),  # type: ignore[arg-type]
        FileArtifactStore(artifact_root),
    )
    profiler = cProfile.Profile()
    profiler.enable()
    service.ingest_path(path, job_id="profile-ingest-cprofile")
    profiler.disable()
    out = io.StringIO()
    stats = pstats.Stats(profiler, stream=out)
    stats.sort_stats("cumulative").print_stats(25)
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--reps", type=int, default=3, help="passes per stage; median reported")
    parser.add_argument(
        "--save", action="store_true", help="write the cProfile table to var/profile/"
    )
    parser.add_argument(
        "--workload",
        choices=(
            "ingest",
            "quality-30000f",
            "validation",
            "aggregation-500k",
            "monitor",
            "api-metrics",
        ),
        default="ingest",
        help="ingest does the stage attribution; the others profile one engine stage",
    )
    args = parser.parse_args()

    if args.workload != "ingest":
        print(profile_other_workload(args.workload))
        return 0

    path = args.path
    if not path.is_file():
        raise SystemExit(f"fixture not found: {path} (see scripts/make_mcap_log.py)")

    with tempfile.TemporaryDirectory(prefix="profile-ingest-") as tmp:
        artifact_root = Path(tmp) / "artifacts"
        stages: dict[str, list[float]] = {
            "container iteration only": [],
            "container + JSON decode": [],
            "reader (decode+flatten+stats+quality)": [],
            "full ingest (read+hash+artifact)": [],
        }
        functions = {
            "container iteration only": stage_container_only,
            "container + JSON decode": stage_container_and_decode,
            "reader (decode+flatten+stats+quality)": stage_reader,
            "full ingest (read+hash+artifact)": lambda p: stage_full_ingest(p, artifact_root),
        }
        for _warmup in range(1):
            for fn in functions.values():
                fn(path)
        for _rep in range(args.reps):
            for name, fn in functions.items():
                stages[name].append(fn(path))

        size_mib = path.stat().st_size / 1024**2
        provenance = collect_provenance(
            {"reps": args.reps, "path": str(path)},
            workload="profile-ingest",
            workload_version="1.0.0",
            dataset=f"{path.name}",
        )

        print(f"fixture: {path.name} ({size_mib:.1f} MiB), reps={args.reps}, median of passes\n")
        print(f"{'stage':<42} {'median s':>10} {'MiB/s':>8}")
        for name, samples in stages.items():
            median = _median(samples)
            print(f"{name:<42} {median:>10.3f} {size_mib / median:>8.1f}")
        print("\nmarginal cost (median deltas):")
        ordered = [
            "container iteration only",
            "container + JSON decode",
            "reader (decode+flatten+stats+quality)",
            "full ingest (read+hash+artifact)",
        ]
        for previous, current in itertools.pairwise(ordered):
            delta = _median(stages[current]) - _median(stages[previous])
            print(f"  {current} <- {previous}: +{delta:.3f} s")

        hotspots = profile_hotspots(path, artifact_root / "cprofile")
        print("\ntop functions by cumulative time:")
        print("\n".join(hotspots.splitlines()[: 6 + 25]))

    if args.save:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = PROFILE_DIR / f"ingest-profile-{stamp}.txt"
        out_path.write_text(hotspots, encoding="utf-8")
        (PROFILE_DIR / f"ingest-profile-{stamp}.json").write_text(
            json.dumps(
                {
                    "provenance": provenance,
                    "fixture": path.name,
                    "size_bytes": path.stat().st_size,
                    "stages_seconds": dict(stages),
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nsaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
