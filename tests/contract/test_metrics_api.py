"""Contract tests for the metrics endpoint and request-latency middleware."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings
from data_engine.jobs.state import JobState


class FakeCatalog:
    def count_jobs(self, state: JobState) -> int:
        return {JobState.QUEUED: 2, JobState.RUNNING: 1}.get(state, 0)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return None

    def count_artifacts(self) -> int:
        return 0

    def count_episodes(self) -> int:
        return 0


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


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def _client(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings(_env_file=None, metrics_path=tmp_path / "runtime.jsonl"),
        initialize_database=False,
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.mark.contract
def test_metrics_endpoint_summarizes_the_sink(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_records(
        tmp_path / "runtime.jsonl",
        [
            _record("jobs_run_time_seconds", 2.0, labels={"job_type": "ingest"}),
            _record("jobs_run_time_seconds", 4.0, labels={"job_type": "ingest"}),
            _record(
                "workers_heartbeat_age_seconds",
                0.0,
                timestamp=(now - timedelta(seconds=15)).isoformat(),
            ),
        ],
    )

    response = _client(tmp_path).get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"]
    assert body["window_seconds"] is None
    assert body["jobs_queue_depth"]["queued"] == 2
    assert body["jobs_queue_depth"]["running"] == 1
    assert body["worker_heartbeat_age_seconds"] == pytest.approx(15.0, abs=5.0)

    summaries = {(s["name"], s["label_key"]): s for s in body["summaries"]}
    run_time = summaries[("jobs_run_time_seconds", "job_type=ingest")]
    assert run_time["count"] == 2
    assert run_time["mean"] == pytest.approx(3.0)
    assert run_time["p95"] == pytest.approx(4.0)

    # The middleware records this request after the response is built, so it lands
    # in the sink but not in this response's summaries.
    assert "jobs_run_time_seconds" in body["series"]
    assert body["series"]["jobs_run_time_seconds"][0]["count"] == 2


@pytest.mark.contract
def test_metrics_endpoint_respects_the_time_window(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_records(
        tmp_path / "runtime.jsonl",
        [
            _record("ancient_metric", 1.0, timestamp=(now - timedelta(hours=3)).isoformat()),
            _record("fresh_metric", 1.0, timestamp=(now - timedelta(seconds=10)).isoformat()),
        ],
    )

    body = _client(tmp_path).get("/api/v1/metrics", params={"window_seconds": 60}).json()

    names = {s["name"] for s in body["summaries"]}
    assert "fresh_metric" in names
    assert "ancient_metric" not in names
    assert body["window_seconds"] == 60.0


@pytest.mark.contract
def test_metrics_endpoint_reports_no_heartbeat_as_null(tmp_path: Path) -> None:
    _write_records(tmp_path / "runtime.jsonl", [_record("jobs_run_time_seconds", 1.0)])
    body = _client(tmp_path).get("/api/v1/metrics").json()
    assert body["worker_heartbeat_age_seconds"] is None


@pytest.mark.contract
def test_request_latency_uses_route_templates_not_raw_paths(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.get("/api/v1/health")
    client.get("/api/v1/jobs/some-long-uuid-value")
    client.get("/no-such-page")

    records = [
        record
        for record in json.loads(
            "["
            + ",".join((tmp_path / "runtime.jsonl").read_text(encoding="utf-8").splitlines())
            + "]"
        )
        if record["name"] == "api_request_duration_seconds"
    ]
    by_route = {record["labels"]["route"]: record["labels"] for record in records}
    assert by_route["/api/v1/health"] == {
        "route": "/api/v1/health",
        "method": "GET",
        "status_class": "2xx",
    }
    assert by_route["/api/v1/jobs/{job_id}"]["status_class"] == "4xx"
    assert by_route["unmatched"]["status_class"] == "4xx"
    # Raw paths and IDs never appear as labels.
    assert not any("some-long-uuid-value" in key for key in by_route)

    counters = [
        record
        for record in json.loads(
            "["
            + ",".join((tmp_path / "runtime.jsonl").read_text(encoding="utf-8").splitlines())
            + "]"
        )
        if record["name"] == "api_requests_total"
    ]
    assert len(counters) == len(records)
