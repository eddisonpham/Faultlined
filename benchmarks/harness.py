"""Reproducible benchmark harness for the synthetic ingest service."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from benchmarks.provenance import ROOT, collect_provenance
from benchmarks.schema import (
    BaselineDocument,
    BaselineHardwareProfile,
    BenchmarkResult,
    Trial,
)
from benchmarks.statistics import summarize
from data_engine.analysis.quality import analyze
from data_engine.ingest.readers.base import ChannelStats, EpisodeExtraction
from data_engine.ingest.service import EpisodeIngestService, SyntheticEpisodeIngestService
from data_engine.observability.aggregate import (
    series as metric_series,
)
from data_engine.observability.aggregate import (
    summarize as metric_summarize,
)
from data_engine.observability.telemetry import sample_resources
from data_engine.storage.artifacts import FileArtifactStore
from data_engine.validation.profile import profile_from_dict
from data_engine.validation.service import ValidationService

BASELINE_DIR = ROOT / "benchmarks" / "baselines"
RESULTS_DIR = ROOT / "benchmarks" / "results"
DEFAULT_BASELINE = BASELINE_DIR / "synthetic-ingest-windows.json"


class BaselineFormatError(ValueError):
    """A baseline file is missing, unreadable, or does not match the baseline schema."""


SYNTHETIC_EPISODE: dict[str, Any] = {
    "task": "bench_pick_place",
    "robot": "synthetic_arm",
    "timestamps": [0.0, 0.1, 0.2, 0.3],
    "observations": [[0.0, 0.0], [0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
    "actions": [[0.01, 0.02], [0.03, 0.04], [0.05, 0.06], [0.07, 0.08]],
}


def run_benchmark(
    operation: Callable[[], Any],
    *,
    name: str,
    version: str = "1.0.0",
    warmups: int = 3,
    trials: int = 10,
    config: dict[str, Any] | None = None,
    dataset: str | None = "synthetic-episode-v1",
    workload_version: str = "1.0.0",
) -> BenchmarkResult:
    if warmups < 0 or trials < 1:
        raise ValueError("warmups must be >= 0 and trials must be >= 1")
    actual_config = config or {"warmups": warmups, "trials": trials}
    run_id = uuid4()
    started = datetime.now(UTC)
    invocation_started = time.perf_counter()
    resource_samples: list[dict[str, Any]] = [sample_resources().to_dict()]
    warmup_failures = 0
    warmups_performed = 0
    for _ in range(warmups):
        warmups_performed += 1
        try:
            operation()
        except Exception:
            warmup_failures += 1
            break
    samples: list[float] = []
    failures = 0
    raw_trials: list[Trial] = []
    if warmup_failures == 0:
        for index in range(trials):
            start_index = len(resource_samples) - 1
            start = time.perf_counter()
            success = True
            try:
                operation()
            except Exception:
                success = False
                failures += 1
            elapsed = time.perf_counter() - start
            samples.append(elapsed)
            resource_samples.append(sample_resources().to_dict())
            raw_trials.append(
                Trial(
                    index=index,
                    latency_seconds=elapsed,
                    success=success,
                    resource_sample_start=start_index,
                    resource_sample_end=len(resource_samples) - 1,
                )
            )
    else:
        resource_samples.append(sample_resources().to_dict())
    summary = summarize(
        samples,
        warmup_count=warmups_performed,
        failures=failures,
        seed=20260928,
        warmup_failure_count=warmup_failures,
    )
    result = BenchmarkResult(
        schema_version=2,
        run_id=run_id,
        benchmark={"name": name, "version": version},
        status="ok" if failures == 0 and warmup_failures == 0 else "failed",
        started_at=started,
        duration_seconds=time.perf_counter() - invocation_started,
        config=actual_config,
        provenance=collect_provenance(
            actual_config,
            workload=name,
            workload_version=workload_version,
            dataset=dataset,
        ),
        trials=raw_trials,
        summary=summary,
        resource_samples=resource_samples,
        failure=None
        if failures == 0 and warmup_failures == 0
        else {
            "type": "WarmupFailure" if warmup_failures else "TrialFailure",
            "message": (
                f"{warmup_failures} warmup operation(s) failed"
                if warmup_failures
                else f"{failures} trial(s) failed"
            ),
        },
    )
    return result


def persist_result(result: BenchmarkResult, directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.run_id}.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def validate_result_file(path: Path) -> BenchmarkResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkResult.model_validate(data)


def _hardware_profile(result: BenchmarkResult) -> dict[str, Any]:
    return BaselineHardwareProfile(
        cpu=result.provenance.hardware.cpu,
        logical_cpus=result.provenance.hardware.logical_cpus,
        ram_bytes=result.provenance.hardware.ram_bytes,
        disk=result.provenance.hardware.disk,
        gpu=result.provenance.hardware.gpu,
        os=result.provenance.software.os,
    ).model_dump(mode="json")


def baseline_document(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark": result.benchmark.model_dump(),
        "hardware_profile": _hardware_profile(result),
        "summary": result.summary.model_dump(mode="json"),
        "config": result.config,
        "source_run_id": str(result.run_id),
    }


def load_baseline(path: Path) -> BaselineDocument:
    """Parse and validate a committed baseline, reporting the file and the offending field.

    Raises BaselineFormatError (a ValueError) rather than KeyError/ValidationError so a
    malformed baseline reads as a baseline problem, not a harness bug.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineFormatError(
            f"baseline {path} does not exist. Create one deliberately with "
            "`just bench --write-baseline --baseline <path>` after an authorized measurement."
        ) from exc
    except json.JSONDecodeError as exc:
        raise BaselineFormatError(
            f"baseline {path} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc
    try:
        return BaselineDocument.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise BaselineFormatError(
            f"baseline {path} does not match the baseline schema: {problems}"
        ) from exc


def compare_to_baseline(
    result: BenchmarkResult, baseline_path: Path = DEFAULT_BASELINE
) -> dict[str, Any]:
    baseline = load_baseline(baseline_path)
    if (
        result.benchmark.name != baseline.benchmark.name
        or result.benchmark.version != baseline.benchmark.version
    ):
        raise ValueError("result benchmark name/version does not match baseline")
    actual_hardware = _hardware_profile(result)
    if actual_hardware != baseline.hardware_profile.model_dump(mode="json"):
        differing = sorted(
            key
            for key in actual_hardware
            if actual_hardware[key] != baseline.hardware_profile.model_dump(mode="json")[key]
        )
        message = "result hardware profile does not match baseline; differing fields: " + ", ".join(
            differing
        )
        if "gpu" in differing:
            # A missing GPU telemetry extra changes the profile without any hardware
            # change, and that is the likeliest cause of a gpu-only mismatch.
            message += (
                "; note that a missing nvidia-ml-py (the 'gpu' extra) is reported as no GPU, "
                "so run `just setup` and use the just recipes, which install extras"
            )
        raise ValueError(message)
    baseline_p50 = baseline.summary.p50_seconds
    current_p50 = result.summary.p50_seconds
    delta = current_p50 - baseline_p50
    percent = (delta / baseline_p50) if baseline_p50 else 0.0
    regression = delta > 0.001 and percent > 0.20
    return {
        "benchmark": result.benchmark.model_dump(),
        "baseline_p50_seconds": baseline_p50,
        "current_p50_seconds": current_p50,
        "delta_seconds": delta,
        "delta_percent": percent,
        "regression": regression,
        "rule": (
            "median increase >20% and >1ms; scaffolding alarm, not a statistical significance claim"
        ),
    }


def write_baseline(result: BenchmarkResult, path: Path = DEFAULT_BASELINE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline_document(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


class _BenchmarkCatalog:
    def __init__(self) -> None:
        self.count = 0

    def register_episode(self, **_kwargs: Any) -> dict[str, str]:
        self.count += 1
        return {"id": f"episode-{self.count}"}

    def record_episode_quality(self, episode_id: str, quality: dict[str, Any]) -> dict[str, str]:
        return {"episode_id": episode_id, "verdict": str(quality["verdict"])}


def _ingest_microbenchmark() -> BenchmarkResult:
    store_root = Path("var/benchmark-artifacts")
    store = FileArtifactStore(store_root)
    catalog = _BenchmarkCatalog()
    service = SyntheticEpisodeIngestService(catalog, store)  # type: ignore[arg-type]
    sequence = 0

    def operation() -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        episode = {**SYNTHETIC_EPISODE, "task": f"bench-pick-{sequence:05d}"}
        return service.ingest(episode, job_id=f"bench-job-{sequence:05d}")

    sample_bytes = len(json.dumps(SYNTHETIC_EPISODE, separators=(",", ":")).encode())
    return run_benchmark(
        operation,
        name="synthetic-episode-ingest",
        config={
            "payload_bytes": sample_bytes,
            "warmups": 3,
            "trials": 10,
            "workers": 1,
            "database": "in-memory benchmark catalog; production filesystem artifact store",
        },
    )


# ------------------------------------------------------- run-intelligence workloads
#
# One workload per feature (backlog B-003, B-012..B-015). Micro workloads follow the
# methodology defaults (3 warmups, 10 trials); the real-data ingest workload is
# end-to-end (1 warmup, 5 trials). Every workload is deterministic: no RNG, no clock
# reads inside the operation.


def _series(frames: int, dims: int = 12) -> dict[str, list[float]]:
    """Deterministic motion-like series: two sin mixtures per dim."""
    return {
        f"action[{d}]": [
            math.sin(0.07 * t + d) + 0.1 * math.sin(0.9 * t + 2 * d) for t in range(frames)
        ]
        for d in range(dims)
    }


def _quality_benchmark(frames: int = 303, *, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    data = _series(frames)
    return run_benchmark(
        lambda: analyze(data),
        name=f"quality-analysis-{frames}f",
        warmups=warmups,
        trials=trials,
        config={
            "frames": frames,
            "dims": 12,
            "warmups": warmups,
            "trials": trials,
            "database": "none; pure analysis over an in-memory series",
        },
        dataset=f"synthetic-series-{frames}f-v1",
    )


def _validation_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    extraction = EpisodeExtraction(
        format="lerobot-v3",
        format_version="v3.0",
        episode_key="episode_index=7",
        source_path=Path("bench.parquet"),
        robot_type="so100_follower",
        task="pick",
        tasks=("pick",),
        frame_count=303,
        duration_seconds=10.0667,
        fps=30.0,
        channels=(
            ChannelStats(name="action", dtype="float32", count=303, min=-2.0, max=80.0),
            ChannelStats(name="observation.state", dtype="float32", count=303, min=-2.0, max=80.0),
        ),
    )
    profile = profile_from_dict(
        {
            "name": "bench-profile",
            "version": "1",
            "required_channels": ["action", "observation.state"],
            "forbidden_channels": ["debug"],
            "min_frames": 10,
            "max_frames": 100_000,
            "min_duration_seconds": 0.5,
            "max_duration_seconds": 3600.0,
            "min_fps": 5.0,
            "max_fps": 120.0,
        }
    )
    samples = {name: [float(i % 7) for i in range(303)] for name in ("action", "observation.state")}
    service = ValidationService(catalog=None)
    return run_benchmark(
        lambda: service.evaluate(extraction, profile, samples=samples),
        name="validation-eval",
        warmups=warmups,
        trials=trials,
        config={
            "rules": 7,
            "frames": 303,
            "channels": 2,
            "warmups": warmups,
            "trials": trials,
            "database": "none; pure rule evaluation",
        },
        dataset="synthetic-extraction-303f-v1",
    )


def _aggregation_benchmark(
    record_count: int = 20_000, *, trials: int = 10, warmups: int = 3
) -> BenchmarkResult:
    start = datetime(2026, 9, 29, tzinfo=UTC)
    label_sets = (
        {"job_type": "ingest", "state": "succeeded"},
        {"job_type": "ingest", "state": "failed"},
        {"job_type": "validate", "state": "succeeded"},
    )
    records = [
        {
            "name": "jobs_run_time_seconds",
            "value": (i % 97) / 1000.0,
            "unit": "seconds",
            "labels": label_sets[i % 3],
            "timestamp": (start + timedelta(seconds=i)).isoformat(),
            "source": "runtime",
            "correlation_id": None,
        }
        for i in range(record_count)
    ]

    def operation() -> Any:
        return metric_summarize(records), metric_series(records, bucket_seconds=60.0)

    return run_benchmark(
        operation,
        name="metrics-aggregation",
        warmups=warmups,
        trials=trials,
        config={
            "records": record_count,
            "bucket_seconds": 60.0,
            "warmups": warmups,
            "trials": trials,
            "database": "none; in-memory record list",
        },
        dataset=f"synthetic-metrics-{record_count}r-v1",
    )


def _summary_model(episodes: int = 50, dims: int = 12) -> dict[str, Any]:
    dim_names = [f"action[{d}]" for d in range(dims)]
    return {
        "episode_count": episodes,
        "verdicts": {
            "smooth": episodes // 2,
            "moderate": episodes // 3,
            "jerky": episodes // 6,
        },
        "length": {
            "count": episodes,
            "mean": 300.0,
            "std": 80.0,
            "min": 200,
            "max": 420,
            "histogram": [
                {"lo": 200 + 22 * i, "hi": 222 + 22 * i, "count": (i * 3) % 11} for i in range(10)
            ],
        },
        "speed_distribution": [
            {
                "episode_id": f"episode-{i}",
                "movement_score": 0.5 + (i % 17) / 10,
                "verdict": "smooth",
            }
            for i in range(episodes)
        ],
        "heat_matrix": {
            "dims": dim_names,
            "episodes": [
                {
                    "episode_id": f"episode-{i}",
                    "values": [((i + d) % 13) / 50 for d in range(dims)],
                }
                for i in range(episodes)
            ],
        },
        "outliers": {"jerk": [], "stall": [], "length": []},
    }


class _BenchApiCatalog:
    """Deterministic read surface for the endpoint render benchmarks (no DB)."""

    def count_jobs(self, _state: Any) -> int:
        return 3

    def quality_summary(self) -> dict[str, Any]:
        return _summary_model()

    def list_episodes(
        self, *, limit: int = 50, state: str | None = None, flag: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "id": f"episode-{i:03d}",
                "episode_key": f"episode_index={i}",
                "format": "lerobot-v3",
                "state": "valid",
                "created_at": "2026-09-29T00:00:00Z",
                "frame_count": 200 + i,
                "movement_score": 0.04,
                "jerk_score": 0.01,
                "stall_ratio": 0.1,
                "verdict": "smooth",
            }
            for i in range(50)
        ]
        if state is not None:
            rows = [row for row in rows if row["state"] == state]
        if flag == "jerky":
            rows = []
        return rows[:limit]

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return {"id": episode_id, "metadata": {}, "state": "valid"}

    def job_report(self, _job_id: str) -> dict[str, Any]:
        return {
            "job": {
                "id": "job-1",
                "type": "ingest",
                "state": "succeeded",
                "payload": {},
                "result": None,
                "error": None,
                "correlation_id": "corr-1",
                "attempts": 1,
                "created_at": "2026-09-29T00:00:00Z",
                "started_at": None,
                "finished_at": None,
            },
            "episodes": {
                "total": 50,
                "by_state": {"valid": 45, "quarantined": 5},
                "verdicts": {"smooth": 30, "moderate": 15, "jerky": 5},
                "flags": {"jerky": 5, "stalled": 3},
                "length": {
                    "count": 50,
                    "mean": 300.0,
                    "std": 80.0,
                    "min": 200,
                    "max": 420,
                    "histogram": [],
                },
                "mean_movement_score": 0.04,
                "mean_jerk_score": 0.01,
                "mean_stall_ratio": 0.1,
            },
            "validation": {
                "episodes_evaluated": 50,
                "results": 50,
                "passed": 45,
                "failed": 5,
                "reason_codes": {"TOO_FEW_FRAMES": 5},
            },
        }

    def get_validation_results(self, _episode_id: str) -> list[dict[str, Any]]:
        return [
            {
                "profile_hash": "a" * 64,
                "profile_name": "staged-strict",
                "profile_version": "1",
                "passed": False,
                "reason_codes": ["TOO_FEW_FRAMES"],
                "violations": [{"code": "TOO_FEW_FRAMES", "message": "too short"}],
                "created_at": "2026-09-29T00:00:00Z",
            }
        ]


def _seed_metrics(path: Path, records: int) -> None:
    start = datetime(2026, 9, 29, tzinfo=UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for i in range(records):
            record = {
                "name": "api_request_duration_seconds",
                "value": (i % 89) / 1000.0,
                "unit": "seconds",
                "labels": {"route": "/api/v1/jobs", "method": "GET", "status_class": "2xx"},
                "timestamp": (start + timedelta(seconds=i)).isoformat(),
                "source": "runtime",
                "correlation_id": None,
            }
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _client_benchmark(
    *, name: str, path: str, dataset: str, trials: int, warmups: int, seed_records: int = 0
) -> BenchmarkResult:
    """In-process request latency via TestClient and a stub catalog (B-014)."""
    from fastapi.testclient import TestClient

    from data_engine.api.app import create_app
    from data_engine.config import Settings

    metrics_path = Path("var/benchmark-artifacts/metrics-scratch.jsonl")
    if seed_records:
        _seed_metrics(metrics_path, seed_records)
    app = create_app(
        Settings(_env_file=None, metrics_path=metrics_path),
        initialize_database=False,
        catalog=_BenchApiCatalog(),  # type: ignore[arg-type]
    )
    client = TestClient(app)
    return run_benchmark(
        lambda: client.get(path).text,
        name=name,
        warmups=warmups,
        trials=trials,
        config={
            "path": path,
            "seed_records": seed_records,
            "warmups": warmups,
            "trials": trials,
            "database": "none; in-process TestClient, stub catalog, no network",
        },
        dataset=dataset,
    )


def _api_metrics_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="api-metrics-endpoint",
        path="/api/v1/metrics",
        dataset="synthetic-metrics-5000r-v1",
        trials=trials,
        warmups=warmups,
        seed_records=5_000,
    )


def _api_episodes_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="api-episodes-catalog",
        path="/api/v1/episodes?limit=50",
        dataset="synthetic-catalog-50e-v1",
        trials=trials,
        warmups=warmups,
    )


def _api_episodes_export_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="api-episodes-export",
        path="/api/v1/episodes/export?limit=50",
        dataset="synthetic-catalog-50e-v1",
        trials=trials,
        warmups=warmups,
    )


def _api_job_report_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="api-job-report",
        path="/api/v1/jobs/job-1/report",
        dataset="synthetic-run-report-50e-v1",
        trials=trials,
        warmups=warmups,
    )


def _api_validation_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="api-episode-validation",
        path="/api/v1/episodes/episode-000/validation",
        dataset="synthetic-validation-results-v1",
        trials=trials,
        warmups=warmups,
    )


def _ui_insights_benchmark(*, trials: int = 10, warmups: int = 3) -> BenchmarkResult:
    return _client_benchmark(
        name="ui-insights-page",
        path="/ui/insights",
        dataset="synthetic-quality-50e-12d-v1",
        trials=trials,
        warmups=warmups,
    )


def _lerobot_benchmark(*, trials: int = 5, warmups: int = 1) -> BenchmarkResult | None:
    """B-003: real LeRobot v3 ingest (read + slice + quality + artifact). Skips when
    the network fixture has not been downloaded (run the `network`-marked tests first)."""
    root = Path("var/real-data/svla_so101_pickplace")
    if not (root / "meta" / "info.json").exists():
        return None
    service = EpisodeIngestService(
        _BenchmarkCatalog(), FileArtifactStore(Path("var/benchmark-artifacts"))
    )
    sequence = 0

    def operation() -> Any:
        nonlocal sequence
        sequence += 1
        return service.ingest_path(
            root, job_id=f"bench-job-{sequence:05d}", episode_key="episode_index=7"
        )

    return run_benchmark(
        operation,
        name="lerobot-ingest-v3",
        warmups=warmups,
        trials=trials,
        config={
            "episode_key": "episode_index=7",
            "frames": 203,
            "warmups": warmups,
            "trials": trials,
            "database": "in-memory benchmark catalog; production filesystem artifact store",
        },
        dataset="lerobot/svla_so101_pickplace tabular slice (v3.0)",
        workload_version="1.0.0",
    )


# Entries are late-bound lambdas so tests can monkeypatch the workload functions.
WORKLOADS: dict[str, Callable[[], BenchmarkResult | None]] = {
    "synthetic-episode-ingest": lambda: _ingest_microbenchmark(),
    "quality-analysis-303f": lambda: _quality_benchmark(303),
    "quality-analysis-3000f": lambda: _quality_benchmark(3000),
    "validation-eval": lambda: _validation_benchmark(),
    "metrics-aggregation": lambda: _aggregation_benchmark(),
    "api-metrics-endpoint": lambda: _api_metrics_benchmark(),
    "api-episodes-catalog": lambda: _api_episodes_benchmark(),
    "api-episode-validation": lambda: _api_validation_benchmark(),
    "api-job-report": lambda: _api_job_report_benchmark(),
    "api-episodes-export": lambda: _api_episodes_export_benchmark(),
    "ui-insights-page": lambda: _ui_insights_benchmark(),
    "lerobot-ingest-v3": lambda: _lerobot_benchmark(),
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="de bench")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--workload",
        choices=("all", *sorted(WORKLOADS)),
        default="all",
        help="which workload to run (default: all; skipped ones need fixtures)",
    )
    args = parser.parse_args(argv)
    if args.write_baseline and args.workload == "all":
        raise SystemExit("--write-baseline needs exactly one --workload name")

    names = sorted(WORKLOADS) if args.workload == "all" else [args.workload]
    reports: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed = False
    regression = False
    for name in names:
        outcome = WORKLOADS[name]()
        if outcome is None:
            skipped.append(name)
            continue
        output = persist_result(outcome)
        report: dict[str, Any] = {
            "workload": name,
            "result": str(output),
            "status": outcome.status,
            "summary": outcome.summary.model_dump(mode="json"),
        }
        if args.write_baseline and outcome.status != "ok":
            raise SystemExit("refusing to write a baseline from a failed benchmark result")
        if outcome.status != "ok":
            report["failure"] = outcome.failure.model_dump() if outcome.failure else None
            failed = True
        elif args.write_baseline:
            report["baseline"] = str(write_baseline(outcome, args.baseline))
        elif name == "synthetic-episode-ingest":
            try:
                comparison = compare_to_baseline(outcome, args.baseline)
                report["comparison"] = comparison
                regression = regression or bool(comparison["regression"])
            except BaselineFormatError as exc:
                print(f"benchmark comparison skipped: {exc}", file=sys.stderr)
                print(f"raw result preserved at {output}", file=sys.stderr)
                report["comparison"] = f"skipped: {exc}"
                failed = True
        else:
            report["comparison"] = "skipped (no committed baseline; results recorded for review)"
        reports.append(report)

    print(json.dumps({"reports": reports, "skipped": skipped}, indent=2))
    if failed or regression:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
