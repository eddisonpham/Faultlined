"""Unit tests for JSONL metric aggregation and the new telemetry emitters."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from data_engine.catalog.repository import _timed_operation
from data_engine.observability.aggregate import (
    format_labels,
    heartbeat_age_seconds,
    newest_metric_at,
    newest_record,
    percentile,
    read_metric_records,
    series,
    summarize,
    window_records,
)
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics, metric_point


def _record(
    name: str,
    value: float,
    *,
    timestamp: str | None = None,
    labels: dict[str, str] | None = None,
    unit: str = "seconds",
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "unit": unit,
        "labels": labels or {},
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "source": "runtime",
        "correlation_id": None,
    }


@pytest.mark.unit
def test_percentile_uses_nearest_rank() -> None:
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 0.95) == 95.0
    assert percentile(values, 0.99) == 99.0
    assert percentile([7.0], 0.99) == 7.0
    with pytest.raises(ValueError):
        percentile([], 0.5)


@pytest.mark.unit
def test_format_labels_is_stable_and_sorted() -> None:
    assert format_labels({"b": "2", "a": "1"}) == "a=1,b=2"
    assert format_labels(None) == ""


@pytest.mark.unit
def test_summarize_groups_by_name_and_labels() -> None:
    records = [
        _record("jobs_run_time_seconds", 1.0, labels={"job_type": "ingest"}),
        _record("jobs_run_time_seconds", 3.0, labels={"job_type": "ingest"}),
        _record("jobs_run_time_seconds", 10.0, labels={"job_type": "validate"}),
        _record("jobs_failures_total", 1.0, labels={"job_type": "ingest"}, unit="count"),
    ]
    summaries = summarize(records)
    assert [(s["name"], s["label_key"]) for s in summaries] == [
        ("jobs_failures_total", "job_type=ingest"),
        ("jobs_run_time_seconds", "job_type=ingest"),
        ("jobs_run_time_seconds", "job_type=validate"),
    ]
    grouped = summaries[1]
    assert grouped["count"] == 2
    assert grouped["min"] == 1.0
    assert grouped["max"] == 3.0
    assert grouped["mean"] == 2.0
    assert grouped["sum"] == 4.0
    assert grouped["p50"] == 1.0
    assert grouped["first_timestamp"] is not None
    assert grouped["last_timestamp"] is not None


@pytest.mark.unit
def test_series_buckets_means_per_metric_name() -> None:
    base = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
    records = [
        _record("api_request_duration_seconds", 1.0, timestamp=base.isoformat()),
        _record(
            "api_request_duration_seconds",
            3.0,
            timestamp=(base + timedelta(seconds=30)).isoformat(),
        ),
        _record(
            "api_request_duration_seconds",
            8.0,
            timestamp=(base + timedelta(seconds=90)).isoformat(),
        ),
    ]
    points = series(records, bucket_seconds=60.0)["api_request_duration_seconds"]
    assert [p["v"] for p in points] == [2.0, 8.0]
    assert [p["count"] for p in points] == [2, 1]
    assert points[0]["t"] == base.isoformat()


@pytest.mark.unit
def test_series_rejects_nonpositive_buckets() -> None:
    with pytest.raises(ValueError):
        series([], bucket_seconds=0)


@pytest.mark.unit
def test_read_metric_records_tolerates_missing_and_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "runtime.jsonl"
    assert read_metric_records(path) == []

    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_record("a", 1.0))
        + "\n"
        + "{not json\n"
        + json.dumps({"no_name": True})
        + "\n"
        + json.dumps(_record("b", 2.0))
        + "\n",
        encoding="utf-8",
    )
    names = [record["name"] for record in read_metric_records(path)]
    assert names == ["a", "b"]
    assert [record["name"] for record in read_metric_records(path, max_records=1)] == ["b"]


@pytest.mark.unit
def test_tail_read_returns_the_newest_records_in_chronological_order(tmp_path: Path) -> None:
    path = tmp_path / "runtime.jsonl"
    path.write_text(
        "\n".join(json.dumps(_record(f"m{i:03d}", float(i))) for i in range(2_500)) + "\n",
        encoding="utf-8",
    )
    tail = read_metric_records(path, max_records=100)
    assert [record["name"] for record in tail] == [f"m{i:03d}" for i in range(2_400, 2_500)]


@pytest.mark.unit
def test_tail_read_across_a_chunk_boundary_is_complete(tmp_path: Path) -> None:
    # Each record ~50 bytes; 2,500 of them span several 1 MiB chunks, so the
    # requested window must survive chunk boundaries without loss or a phantom.
    path = tmp_path / "runtime.jsonl"
    path.write_text(
        "\n".join(json.dumps(_record(f"m{i:05d}", float(i))) for i in range(30_000)) + "\n",
        encoding="utf-8",
    )
    tail = read_metric_records(path, max_records=100)
    assert [record["name"] for record in tail] == [f"m{i:05d}" for i in range(29_900, 30_000)]


@pytest.mark.unit
def test_tail_read_skips_corrupt_lines_like_the_forward_read(tmp_path: Path) -> None:
    good = [json.dumps(_record(f"g{i}", float(i))) for i in range(10)]
    path = tmp_path / "runtime.jsonl"
    path.write_text(
        "\n".join([*good[:5], "{corrupt", "", '{"no_name": 1}', *good[5:]]) + "\n",
        encoding="utf-8",
    )
    tail = read_metric_records(path, max_records=4)
    # Newest four *valid* records: g6..g9 (g5 sits behind g6..g9 in the tail).
    assert [record["name"] for record in tail] == ["g6", "g7", "g8", "g9"]


@pytest.mark.unit
def test_tail_read_is_a_tail_not_a_full_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The optimization is the point (EXP-0007): cost must track the window, not
    # the history. Pin the load-bearing boundary, then verify the shrink.
    from data_engine.observability import aggregate

    monkeypatch.setattr(aggregate, "TAIL_MAX_BYTES", 4096)
    path = tmp_path / "runtime.jsonl"
    path.write_text(
        "\n".join(json.dumps(_record(f"m{i:06d}", float(i))) for i in range(100_000)) + "\n",
        encoding="utf-8",
    )
    tail = read_metric_records(path, max_records=4)
    assert [record["name"] for record in tail] == ["m099996", "m099997", "m099998", "m099999"]


@pytest.mark.unit
def test_tail_read_handles_undersized_and_oversized_windows(tmp_path: Path) -> None:
    path = tmp_path / "runtime.jsonl"
    path.write_text(json.dumps(_record("only", 1.0)) + "\n", encoding="utf-8")
    assert [r["name"] for r in read_metric_records(path, max_records=100)] == ["only"]
    assert read_metric_records(path, max_records=0) == []
    assert read_metric_records(tmp_path / "absent.jsonl", max_records=10) == []


@pytest.mark.unit
def test_window_records_filters_by_timestamp() -> None:
    now = datetime.now(UTC)
    old = _record("old", 1.0, timestamp=(now - timedelta(hours=2)).isoformat())
    fresh = _record("fresh", 1.0, timestamp=(now - timedelta(seconds=5)).isoformat())
    kept = window_records([old, fresh], since=now - timedelta(seconds=60))
    assert [record["name"] for record in kept] == ["fresh"]
    assert len(window_records([old, fresh], since=None)) == 2


@pytest.mark.unit
def test_heartbeat_age_comes_from_newest_record() -> None:
    now = datetime.now(UTC)
    records = [
        _record(
            "workers_heartbeat_age_seconds",
            0.0,
            timestamp=(now - timedelta(seconds=30)).isoformat(),
        ),
        _record(
            "workers_heartbeat_age_seconds",
            0.0,
            timestamp=(now - timedelta(seconds=10)).isoformat(),
        ),
    ]
    assert heartbeat_age_seconds(records, now=now) == pytest.approx(10.0)
    assert heartbeat_age_seconds([], now=now) is None
    # A clock-skewed future heartbeat must not produce a negative age.
    future = [
        _record(
            "workers_heartbeat_age_seconds", 0.0, timestamp=(now + timedelta(seconds=5)).isoformat()
        )
    ]
    assert heartbeat_age_seconds(future, now=now) == 0.0


@pytest.mark.unit
def test_newest_record_picks_the_latest_of_one_metric() -> None:
    """The read-back that lets one process report what another one is doing."""
    base = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    records = [
        _record("monitor_tick_seconds", 1.0, timestamp=base.isoformat()),
        _record("other", 9.0, timestamp=(base + timedelta(hours=1)).isoformat()),
        _record("monitor_tick_seconds", 3.0, timestamp=(base + timedelta(seconds=30)).isoformat()),
        _record("monitor_tick_seconds", 2.0, timestamp=(base + timedelta(minutes=1)).isoformat()),
    ]
    newest = newest_record(records, name="monitor_tick_seconds")
    assert newest is not None
    assert newest["value"] == 2.0
    assert newest_metric_at(records, name="monitor_tick_seconds") == base + timedelta(minutes=1)
    assert newest_record(records, name="absent") is None
    assert newest_metric_at(records, name="absent") is None
    # A record with no readable timestamp cannot be reported as a time.
    assert newest_metric_at([_record("m", 1.0, timestamp="not a time")], name="m") is None


@pytest.mark.unit
def test_api_request_emitter_writes_duration_and_counter(tmp_path: Path) -> None:
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "runtime.jsonl"))
    metrics.api_request(0.02, route="/api/v1/jobs/{job_id}", method="GET", status_class="2xx")
    records = read_metric_records(tmp_path / "runtime.jsonl")
    by_name = {record["name"]: record for record in records}
    assert by_name["api_request_duration_seconds"]["value"] == pytest.approx(0.02)
    assert by_name["api_requests_total"]["value"] == 1.0
    for record in records:
        assert record["labels"] == {
            "route": "/api/v1/jobs/{job_id}",
            "method": "GET",
            "status_class": "2xx",
        }


@pytest.mark.unit
def test_catalog_query_and_heartbeat_emitters(tmp_path: Path) -> None:
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "runtime.jsonl"))
    metrics.catalog_query(0.001, operation="claim_job")
    metrics.worker_heartbeat(worker_state="busy")
    records = read_metric_records(tmp_path / "runtime.jsonl")
    assert records[0]["labels"] == {"operation": "claim_job"}
    assert records[1]["name"] == "workers_heartbeat_age_seconds"
    assert records[1]["value"] == 0.0
    assert records[1]["labels"] == {"worker_state": "busy"}


@pytest.mark.unit
def test_emitted_points_still_reject_high_cardinality_labels() -> None:
    with pytest.raises(ValueError):
        metric_point("api_request_duration_seconds", 1.0, "seconds", labels={"job_id": "x"})


class _StubRepo:
    def __init__(self, metrics: RuntimeMetrics) -> None:
        self._metrics = metrics

    def quick_op(self, factor: int) -> int:
        return factor * 2

    def failing_op(self) -> None:
        raise ValueError("boom")


@pytest.mark.unit
def test_timed_operation_records_query_duration(tmp_path: Path) -> None:
    metrics = RuntimeMetrics(JsonlMetricSink(tmp_path / "runtime.jsonl"))
    stub = _StubRepo(metrics)

    wrapped = _timed_operation(_StubRepo.quick_op)
    assert wrapped(stub, 21) == 42

    with pytest.raises(ValueError):
        _timed_operation(_StubRepo.failing_op)(stub)

    records = read_metric_records(tmp_path / "runtime.jsonl")
    assert [record["labels"]["operation"] for record in records] == ["quick_op", "failing_op"]
    assert all(record["name"] == "catalog_query_duration_seconds" for record in records)
    assert all(record["value"] >= 0 for record in records)
