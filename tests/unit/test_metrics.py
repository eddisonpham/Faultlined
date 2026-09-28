import json
from pathlib import Path
from typing import Any

import pytest

from data_engine.observability.metrics import (
    JsonlMetricSink,
    RuntimeMetrics,
    Timer,
    metric_point,
)


@pytest.mark.unit
def test_metric_point_validation_and_jsonl_sink(tmp_path: Path) -> None:
    point = metric_point("jobs_run_time_seconds", 0.25, "seconds", labels={"job_type": "ingest"})
    sink = JsonlMetricSink(tmp_path / "metrics.jsonl")
    sink.write(point)
    stored = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert stored["name"] == "jobs_run_time_seconds"
    assert stored["labels"] == {"job_type": "ingest"}
    with pytest.raises(ValueError, match="lowercase"):
        metric_point("BadName", 1, "count")
    with pytest.raises(ValueError, match="high-cardinality"):
        metric_point("jobs_total", 1, "count", labels={"job_id": "job-1"})


@pytest.mark.unit
def test_timer_records_nonnegative_monotonic_duration() -> None:
    with Timer() as timer:
        sum(range(100))
    assert timer.elapsed_seconds >= 0


class _BrokenSink:
    def write(self, _point: Any) -> None:
        raise OSError("disk unavailable")


@pytest.mark.unit
def test_runtime_metrics_without_sink_is_a_noop() -> None:
    metrics = RuntimeMetrics()
    metrics.queue_depth(3)
    metrics.job_queue_time(1.0, job_type="ingest")
    metrics.job_run_time(1.0, job_type="ingest", state="succeeded")
    metrics.job_failure(job_type="ingest", reason_code="INGEST_PARSE_FAILED")
    metrics.stage_duration(1.0, stage="ingest", status="succeeded")
    metrics.episode_ingested(episode_format="synthetic-json", status="succeeded")
    metrics.artifact_written(128)


@pytest.mark.unit
def test_runtime_metrics_swallows_sink_failures() -> None:
    """ADR 0008: telemetry absence must never break the pipeline."""
    metrics = RuntimeMetrics(_BrokenSink())  # type: ignore[arg-type]
    metrics.queue_depth(3)
    metrics.artifact_written(128)


@pytest.mark.unit
def test_runtime_metrics_emits_registry_names(tmp_path: Path) -> None:
    sink = JsonlMetricSink(tmp_path / "metrics.jsonl")
    metrics = RuntimeMetrics(sink)
    metrics.queue_depth(2)
    metrics.job_queue_time(0.5, job_type="ingest")
    metrics.job_run_time(0.25, job_type="ingest", state="succeeded")
    metrics.job_failure(job_type="ingest", reason_code="INGEST_PARSE_FAILED")
    metrics.stage_duration(0.25, stage="ingest", status="succeeded")
    metrics.episode_ingested(episode_format="synthetic-json", status="succeeded")
    metrics.artifact_written(4096)

    lines = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    names = [json.loads(line)["name"] for line in lines]
    assert names == [
        "jobs_queue_depth",
        "jobs_queue_time_seconds",
        "jobs_run_time_seconds",
        "jobs_failures_total",
        "pipeline_stage_duration_seconds",
        "episodes_ingested_total",
        "artifacts_written_bytes_total",
    ]
    assert [json.loads(line)["unit"] for line in lines] == [
        "jobs",
        "seconds",
        "seconds",
        "count",
        "seconds",
        "episodes",
        "bytes",
    ]
