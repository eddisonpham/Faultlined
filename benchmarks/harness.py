"""Reproducible benchmark harness for the synthetic ingest service."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
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
from data_engine.ingest.service import SyntheticEpisodeIngestService
from data_engine.observability.telemetry import sample_resources
from data_engine.storage.artifacts import FileArtifactStore

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="de bench")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    result = _ingest_microbenchmark()
    output = persist_result(result)
    if args.write_baseline:
        if result.status != "ok":
            raise SystemExit("refusing to write a baseline from a failed benchmark result")
        baseline_path = write_baseline(result, args.baseline)
        print(
            json.dumps(
                {
                    "result": str(output),
                    "baseline": str(baseline_path),
                    "summary": result.summary.model_dump(),
                },
                indent=2,
            )
        )
        return
    if result.status != "ok":
        print(
            json.dumps(
                {
                    "result": str(output),
                    "status": result.status,
                    "failure": result.failure.model_dump(),
                },
                indent=2,
            )
        )
        raise SystemExit(1)
    try:
        comparison = compare_to_baseline(result, args.baseline)
    except BaselineFormatError as exc:
        print(f"benchmark comparison skipped: {exc}", file=sys.stderr)
        print(f"raw result preserved at {output}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {
                "result": str(output),
                "summary": result.summary.model_dump(),
                "comparison": comparison,
            },
            indent=2,
        )
    )
    if comparison["regression"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
