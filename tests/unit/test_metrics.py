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
from data_engine.observability.telemetry import TelemetrySample


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


def _sample(**overrides: Any) -> TelemetrySample:
    """A host sample with every field readable, then overridden per test."""
    fields: dict[str, Any] = {
        "timestamp": "2026-10-01T00:00:00+00:00",
        "cpu_percent": 12.5,
        "process_rss_bytes": 1_000,
        "memory_used_bytes": 2_000,
        "memory_available_bytes": 3_000,
        "disk_used_bytes": 4_000,
        "disk_free_bytes": 5_000,
        "network_bytes_sent_total": 6_000,
        "network_bytes_recv_total": 7_000,
        "gpu_present": False,
    }
    fields.update(overrides)
    return TelemetrySample(**fields)


@pytest.mark.unit
def test_host_gauges_are_emitted_and_unreadable_fields_are_skipped(tmp_path: Path) -> None:
    """The `system_*` registry rows must exist as records, not just as documentation."""
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "metrics.jsonl"))

    metrics.emit_host_sample(
        _sample(cpu_percent=None, network_bytes_recv_total=None),
    )

    points = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    by_name = {point["name"]: point for point in points}
    assert set(by_name) == {
        "system_memory_used_bytes",
        "process_rss_bytes",
        "system_disk_free_bytes",
        "system_network_bytes_sent_total",
    }
    assert by_name["process_rss_bytes"]["labels"] == {"process_role": "worker"}
    assert by_name["system_disk_free_bytes"]["labels"] == {"volume": "."}
    assert by_name["system_network_bytes_sent_total"]["labels"] == {"interface": "aggregate"}
    # No GPU answered, so no zeroed GPU record: absent beats fabricated.
    assert "system_gpu_utilization_percent" not in by_name


@pytest.mark.unit
def test_gpu_gauges_appear_only_when_a_device_answers(tmp_path: Path) -> None:
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "metrics.jsonl"))

    metrics.emit_host_sample(
        _sample(gpu_present=True, gpu_utilization_percent=40.0, gpu_memory_used_bytes=8_000)
    )

    points = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    gpu = {point["name"]: point for point in points if point["name"].startswith("system_gpu")}
    assert gpu["system_gpu_utilization_percent"]["value"] == 40.0
    assert gpu["system_gpu_memory_used_bytes"]["labels"] == {"device_index": "0"}


@pytest.mark.unit
def test_a_host_sample_that_cannot_be_taken_is_logged_and_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0008 again: the sampler runs in the worker loop, so it must not raise."""

    def _broken(**_kwargs: Any) -> TelemetrySample:
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr("data_engine.observability.metrics.sample_resources", _broken)
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "metrics.jsonl"))

    metrics.sample_host()

    assert not (tmp_path / "metrics.jsonl").exists()
