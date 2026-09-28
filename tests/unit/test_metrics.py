import json
from pathlib import Path

import pytest

from data_engine.observability.metrics import JsonlMetricSink, Timer, metric_point


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
