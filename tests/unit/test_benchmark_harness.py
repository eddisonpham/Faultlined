import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from benchmarks.harness import (
    compare_to_baseline,
    persist_result,
    run_benchmark,
    validate_result_file,
    write_baseline,
)
from benchmarks.provenance import collect_provenance
from benchmarks.schema import BenchmarkResult
from pydantic import ValidationError

SCHEMA_PATH = Path(__file__).parents[2] / "agents" / "benchmarking" / "result.schema.json"


def _result(p50: float = 0.01) -> BenchmarkResult:
    config = {"warmups": 1, "trials": 2}
    provenance = collect_provenance(config, workload="test", workload_version="1.0")
    return BenchmarkResult.model_validate(
        {
            "schema_version": 2,
            "run_id": str(uuid4()),
            "benchmark": {"name": "test-bench", "version": "1.0"},
            "status": "ok",
            "started_at": "2026-09-28T00:00:00Z",
            "duration_seconds": 1.0,
            "config": config,
            "provenance": provenance,
            "trials": [
                {
                    "index": 0,
                    "latency_seconds": p50,
                    "success": True,
                    "resource_sample_start": 0,
                    "resource_sample_end": 1,
                },
                {
                    "index": 1,
                    "latency_seconds": p50,
                    "success": True,
                    "resource_sample_start": 1,
                    "resource_sample_end": 2,
                },
            ],
            "summary": {
                "n": 2,
                "warmup_count": 1,
                "warmup_failure_count": 0,
                "p50_seconds": p50,
                "p95_seconds": p50,
                "p99_seconds": p50,
                "mean_seconds": p50,
                "stddev_seconds": 0.0,
                "ci95_mean_seconds": [p50, p50],
                "failure_count": 0,
            },
            "resource_samples": [
                {"cpu_percent": 0.0},
                {"cpu_percent": 0.0},
                {"cpu_percent": 0.0},
            ],
            "failure": None,
        }
    )


@pytest.mark.unit
def test_result_rejects_missing_provenance() -> None:
    payload = _result().model_dump(mode="json")
    del payload["provenance"]["git_commit"]
    with pytest.raises(ValidationError):
        BenchmarkResult.model_validate(payload)


@pytest.mark.unit
def test_published_json_schema_matches_pydantic_model() -> None:
    published_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert published_schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert published_schema.pop("$id") == (
        "https://example.invalid/data-engine/benchmark-result-v2.schema.json"
    )
    assert published_schema == BenchmarkResult.model_json_schema()


@pytest.mark.unit
def test_harness_runs_warmup_and_repeated_trials_and_persists(tmp_path: Path) -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1

    with (
        patch("benchmarks.harness.sample_resources") as sample,
        patch(
            "benchmarks.harness.collect_provenance",
            return_value=collect_provenance({}, workload="op", workload_version="1"),
        ),
    ):
        sample.return_value.to_dict.return_value = {"gpu_present": False}
        result = run_benchmark(operation, name="op", warmups=2, trials=3)
    assert calls == 5
    assert result.summary.n == 3
    output = persist_result(result, tmp_path)
    assert validate_result_file(output).run_id == result.run_id


@pytest.mark.unit
def test_warmup_failure_produces_persistable_failed_result(tmp_path: Path) -> None:
    def operation() -> None:
        raise RuntimeError("synthetic warmup error")

    with (
        patch("benchmarks.harness.sample_resources") as sample,
        patch(
            "benchmarks.harness.collect_provenance",
            return_value=collect_provenance({}, workload="op", workload_version="1"),
        ),
    ):
        sample.return_value.to_dict.return_value = {"gpu_present": False}
        result = run_benchmark(operation, name="op", warmups=3, trials=2)

    assert result.status == "failed"
    assert result.trials == []
    assert result.summary.n == 0
    assert result.summary.warmup_count == 1
    assert result.summary.warmup_failure_count == 1
    assert result.summary.failure_count == 0
    assert result.failure is not None
    assert result.failure.type == "WarmupFailure"
    output = persist_result(result, tmp_path)
    assert validate_result_file(output).run_id == result.run_id


@pytest.mark.unit
def test_baseline_comparison_flags_synthetic_regression(tmp_path: Path) -> None:
    baseline_path = write_baseline(_result(0.01), tmp_path / "baseline.json")
    comparison = compare_to_baseline(_result(0.02), baseline_path)
    assert comparison["regression"] is True
    assert comparison["delta_percent"] == pytest.approx(1.0)


@pytest.mark.unit
def test_benchmark_marks_failed_trial_as_failed() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic failure")

    with (
        patch("benchmarks.harness.sample_resources") as sample,
        patch(
            "benchmarks.harness.collect_provenance",
            return_value=collect_provenance({}, workload="op", workload_version="1"),
        ),
    ):
        sample.return_value.to_dict.return_value = {"gpu_present": False}
        result = run_benchmark(operation, name="op", warmups=0, trials=2)

    assert result.status == "failed"
    assert result.summary.failure_count == 1
    assert result.failure is not None
    assert result.failure.type == "TrialFailure"


@pytest.mark.unit
def test_result_rejects_summary_that_disagrees_with_trials() -> None:
    payload = _result().model_dump(mode="json")
    payload["summary"]["p50_seconds"] = 99.0
    with pytest.raises(ValidationError, match="p50_seconds"):
        BenchmarkResult.model_validate(payload)


@pytest.mark.unit
def test_baseline_comparison_rejects_other_workload(tmp_path: Path) -> None:
    baseline_path = write_baseline(_result(), tmp_path / "baseline.json")
    changed_payload = _result().model_dump(mode="json")
    changed_payload["benchmark"] = {"name": "different", "version": "1.0"}
    changed = BenchmarkResult.model_validate(changed_payload)
    with pytest.raises(ValueError, match="name/version"):
        compare_to_baseline(changed, baseline_path)


@pytest.mark.unit
def test_baseline_comparison_rejects_other_hardware(tmp_path: Path) -> None:
    baseline_path = write_baseline(_result(), tmp_path / "baseline.json")
    changed_payload = _result().model_dump(mode="json")
    changed_payload["provenance"]["hardware"]["logical_cpus"] += 1
    changed = BenchmarkResult.model_validate(changed_payload)
    with pytest.raises(ValueError, match="hardware profile"):
        compare_to_baseline(changed, baseline_path)


@pytest.mark.unit
def test_failed_trial_result_is_consistent_and_invalid_success_is_rejected() -> None:
    payload = _result().model_dump(mode="json")
    payload["trials"][1]["success"] = False
    payload["summary"]["failure_count"] = 1
    payload["status"] = "failed"
    payload["failure"] = {"type": "TrialFailure", "message": "one trial failed"}
    assert BenchmarkResult.model_validate(payload).status == "failed"

    payload["status"] = "ok"
    payload["failure"] = None
    with pytest.raises(ValidationError, match="status must match"):
        BenchmarkResult.model_validate(payload)
