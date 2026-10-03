from pathlib import Path
from typing import Any

import pytest

from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics


class FakeCatalog:
    """In-memory stand-in covering the lifecycle methods the worker calls."""

    def __init__(self, job: dict[str, Any] | None) -> None:
        self.job = job
        self.state = "queued" if job else None
        self.episode: dict[str, Any] | None = None
        self.queued = 1 if job else 0
        self.cancel_requested = False
        self.requeues = 0

    def count_jobs(self, state: JobState) -> int:
        return self.queued

    def claim_job(self) -> dict[str, Any] | None:
        if self.job is None:
            return None
        self.state = "running"
        if self.job is not None:
            self.job["attempts"] = int(self.job.get("attempts", 0)) + 1
        return self.job

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.cancel_requested

    def requeue_for_retry(self, job_id: str) -> dict[str, Any]:
        self.state = JobState.QUEUED.value
        self.requeues += 1
        return {"id": job_id, "state": self.state}

    def mark_deadline_expired(self, job_id: str) -> dict[str, Any]:
        self.state = JobState.TIMED_OUT.value
        self.error = {"type": "DeadlineExceeded", "message": "job exceeded its deadline"}
        return {"id": job_id, "state": self.state}

    def register_episode(self, **kwargs: Any) -> dict[str, Any]:
        self.episode = {
            "id": "episode-1",
            "artifact_hash": kwargs["artifact_hash"],
            "metadata": kwargs["metadata"],
        }
        return self.episode

    def record_episode_quality(self, episode_id: str, quality: dict[str, Any]) -> dict[str, Any]:
        self.quality = quality
        return {"episode_id": episode_id, "verdict": quality["verdict"]}

    def finish_job(self, job_id: str, state: JobState, **kwargs: Any) -> dict[str, Any]:
        self.state = state.value
        if kwargs.get("error") is not None:
            self.error = kwargs["error"]
        return {"id": job_id, "state": state.value, **kwargs}

    def get_job(self, job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"id": job_id, "state": self.state}
        if hasattr(self, "error"):
            result["error"] = self.error
        return result


def _job(
    episode: dict[str, Any] | None = None,
    *,
    attempts: int = 1,
    max_attempts: int = 1,
    deadline_at: Any = None,
) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    created = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    return {
        "id": "job-1",
        "type": "ingest",
        "state": "running",
        "correlation_id": "corr-1",
        "attempts": attempts,
        "max_attempts": max_attempts,
        "created_at": created,
        "started_at": created + timedelta(seconds=2),
        "payload": {
            "episode": episode
            or {
                "task": "pick",
                "robot": "test-arm",
                "timestamps": [0.0, 0.1],
                "observations": [[0.0], [1.0]],
                "actions": [[0.1], [0.2]],
            }
        },
        "deadline_at": deadline_at,
    }


@pytest.mark.unit
def test_worker_processes_episode_and_stores_content_addressed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job())
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))

    result = worker.process_one()

    assert result is not None
    assert result["state"] == "succeeded"
    assert result["result"]["episode_id"] == "episode-1"
    assert catalog.episode is not None
    assert worker.artifacts.get_bytes(result["result"]["artifact_hash"])


@pytest.mark.unit
def test_worker_ingests_a_real_dataset_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `ingest_source` job type routes a path through the reader, not the API body."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "ds"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(
        '{"codebase_version": "v3.0", "robot_type": "arm", "fps": 10, "features": {}}',
        encoding="utf-8",
    )
    data_dir = root / "data" / "chunk-000"
    data_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "action": [[0.0], [0.5], [1.0]],
                "timestamp": [0.0, 1.0, 2.0],
                "frame_index": [0, 1, 2],
                "episode_index": [0, 0, 0],
                "index": [0, 1, 2],
                "task_index": [0, 0, 0],
            }
        ),
        data_dir / "file-000.parquet",
    )
    index_dir = root / "meta" / "episodes" / "chunk-000"
    index_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "episode_index": [0],
                "data/chunk_index": [0],
                "data/file_index": [0],
                "dataset_from_index": [0],
                "dataset_to_index": [3],
                "tasks": [["reach"]],
                "length": [3],
            }
        ),
        index_dir / "file-000.parquet",
    )

    job = _job()
    job["type"] = "ingest_source"
    job["payload"] = {"source": str(root), "episode_key": "episode_index=0"}
    catalog = FakeCatalog(job)
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(Settings(artifact_root=tmp_path / "store", _env_file=None))

    result = worker.process_one()

    assert result is not None
    assert result["state"] == "succeeded"
    assert result["result"]["format"] == "lerobot-v3"
    assert result["result"]["frame_count"] == 3
    assert worker.artifacts.get_bytes(result["result"]["artifact_hash"])


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload", [{}, {"source": ""}, {"source": 5}, {"source": "x", "episode_key": 1}]
)
def test_worker_rejects_a_malformed_source_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    job = _job()
    job["type"] = "ingest_source"
    job["payload"] = payload
    catalog = FakeCatalog(job)
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))

    assert worker.process_one()["state"] == "failed"


@pytest.mark.unit
def test_worker_reports_a_missing_source_file_as_an_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job()
    job["type"] = "ingest_source"
    job["payload"] = {"source": str(tmp_path / "nope")}
    catalog = FakeCatalog(job)
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )

    assert worker.process_one()["state"] == "failed"
    failures = [p for p in _points(metrics_path) if p["name"] == "jobs_failures_total"]
    assert failures[0]["labels"]["reason_code"] == "INGEST_PARSE_FAILED"


@pytest.mark.unit
def test_worker_returns_none_when_no_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = FakeCatalog(None)
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))
    assert worker.process_one() is None


@pytest.mark.unit
def test_worker_marks_invalid_job_failed_without_leaking_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job({"task": "missing-fields"}))
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))

    result = worker.process_one()

    assert result is not None
    assert result["state"] == "failed"
    assert result["error"]["message"] == "job handler failed"


def _points(path: Path) -> list[dict[str, Any]]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.unit
def test_worker_emits_queue_job_and_stage_metrics_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job())
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )

    result = worker.process_one()

    assert result is not None
    points = _points(metrics_path)
    by_name = {point["name"]: point for point in points}
    assert by_name["jobs_queue_depth"]["value"] == 1
    assert by_name["jobs_queue_depth"]["labels"] == {"state": "queued"}
    assert by_name["jobs_queue_time_seconds"]["value"] == pytest.approx(2.0)
    assert by_name["jobs_queue_time_seconds"]["labels"] == {"job_type": "ingest"}
    assert by_name["jobs_run_time_seconds"]["labels"] == {
        "job_type": "ingest",
        "state": "succeeded",
    }
    assert by_name["pipeline_stage_duration_seconds"]["labels"] == {
        "stage": "ingest",
        "status": "succeeded",
    }
    assert by_name["episodes_ingested_total"]["labels"] == {
        "format": "synthetic-json",
        "status": "succeeded",
    }
    assert by_name["artifacts_written_bytes_total"]["value"] > 0
    assert by_name["artifacts_written_bytes_total"]["labels"] == {"kind": "blob"}
    assert by_name["workers_heartbeat_age_seconds"]["labels"] == {"worker_state": "busy"}
    assert all(point["source"] == "runtime" for point in points)


@pytest.mark.unit
def test_worker_emits_failure_metric_with_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job({"task": "missing-fields"}))
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )

    worker.process_one()

    points = {point["name"]: point for point in _points(metrics_path)}
    assert points["jobs_failures_total"]["labels"] == {
        "job_type": "ingest",
        "reason_code": "INGEST_PARSE_FAILED",
    }
    assert points["jobs_run_time_seconds"]["labels"]["state"] == "failed"
    assert points["pipeline_stage_duration_seconds"]["labels"]["status"] == "failed"
    assert "episodes_ingested_total" not in points


@pytest.mark.unit
def test_worker_records_queue_depth_when_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(None)
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )

    assert worker.process_one() is None

    points = _points(metrics_path)
    assert [point["name"] for point in points] == [
        "jobs_queue_depth",
        "workers_heartbeat_age_seconds",
    ]
    assert points[-1]["labels"] == {"worker_state": "idle"}


@pytest.mark.unit
def test_metrics_do_not_carry_job_or_episode_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job())
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )

    worker.process_one()

    forbidden = {"job_id", "episode_id", "correlation_id"}
    for point in _points(metrics_path):
        assert not forbidden & set(point["labels"])


def _failing_worker(
    catalog: FakeCatalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[IngestWorker, Path]:
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)),
    )
    return worker, metrics_path


class _TransientFailure:
    """Stands in for a failure that may not repeat: a lost connection, a busy disk."""

    def __init__(self) -> None:
        self.calls = 0

    def ingest(self, episode: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        self.calls += 1
        raise ConnectionResetError("the artifact store went away")


def _flaky_worker(
    catalog: FakeCatalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[IngestWorker, Path, _TransientFailure]:
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)
    failure = _TransientFailure()
    monkeypatch.setattr(worker.ingest, "ingest", failure.ingest)
    return worker, metrics_path, failure


@pytest.mark.unit
def test_worker_requeues_while_attempts_remain_then_fails_when_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: a transient failure is retried up to max_attempts, then parked as failed."""
    catalog = FakeCatalog(_job(attempts=0, max_attempts=2))
    worker, metrics_path, _ = _flaky_worker(catalog, tmp_path, monkeypatch)

    worker.process_one()

    assert catalog.state == JobState.QUEUED.value
    assert catalog.requeues == 1
    assert {point["name"] for point in _points(metrics_path)} >= {
        "jobs_retries_total",
        "jobs_failures_total",
    }

    # Second attempt is the last one: no requeue, the job settles as failed.
    worker.process_one()

    assert catalog.state == JobState.FAILED.value
    assert catalog.requeues == 1
    retries = [p for p in _points(metrics_path) if p["name"] == "jobs_retries_total"]
    assert [point["labels"]["attempt"] for point in retries] == ["1"]


@pytest.mark.unit
def test_worker_cancels_a_failing_job_when_cancellation_was_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: a cancel request wins over a would-be retry."""
    catalog = FakeCatalog(_job({"task": "missing-fields"}, attempts=0, max_attempts=3))
    catalog.cancel_requested = True
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.CANCELED.value
    assert catalog.requeues == 0
    assert "jobs_cancellations_total" in {point["name"] for point in _points(metrics_path)}


@pytest.mark.unit
def test_worker_does_not_retry_a_failure_that_cannot_improve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: unparseable data is terminal, so the attempt budget is not spent on it."""
    job = _job({"task": "missing-fields"}, attempts=0, max_attempts=5)
    catalog = FakeCatalog(job)
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.FAILED.value
    assert catalog.requeues == 0, "an identical second attempt cannot parse the same bytes"
    assert "jobs_retries_total" not in {point["name"] for point in _points(metrics_path)}


@pytest.mark.unit
def test_worker_does_not_retry_a_malformed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: a profile the constructor rejects fails identically on every attempt.

    Terminal like F1, but with the reason code that names the operator's own
    mistake (`VALIDATION_PROFILE_INVALID`) instead of `INTERNAL_ERROR`.
    """
    job = _job(attempts=0, max_attempts=3)
    job["type"] = "validate"
    job["payload"] = {
        "profile": {"name": "staged", "version": "1", "min_frames": 10, "max_frames": 5},
        "episode_ids": ["ep-1"],
    }
    catalog = FakeCatalog(job)
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.FAILED.value
    assert result["error"]["type"] == "InvalidProfile"
    assert catalog.requeues == 0, "a malformed profile fails the same way twice"
    failures = [p for p in _points(metrics_path) if p["name"] == "jobs_failures_total"]
    assert failures[0]["labels"]["reason_code"] == "VALIDATION_PROFILE_INVALID"
    assert "jobs_retries_total" not in {p["name"] for p in _points(metrics_path)}


@pytest.mark.unit
def test_worker_retries_a_transient_failure_with_budget_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: an unexpected error is retried, because it may not repeat."""
    catalog = FakeCatalog(_job(attempts=0, max_attempts=3))
    worker, _, failure = _flaky_worker(catalog, tmp_path, monkeypatch)

    assert worker.process_one()["state"] == JobState.QUEUED.value
    assert catalog.requeues == 1
    assert failure.calls == 1


@pytest.mark.unit
def test_worker_times_out_a_job_whose_deadline_already_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    catalog = FakeCatalog(
        _job(attempts=0, max_attempts=3, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.TIMED_OUT.value
    assert result["error"]["type"] == "DeadlineExceeded"
    # A job that never ran must not be recorded as a handler failure.
    assert "jobs_failures_total" not in {point["name"] for point in _points(metrics_path)}
    assert "jobs_timeouts_total" in {point["name"] for point in _points(metrics_path)}


@pytest.mark.unit
def test_worker_leaves_a_future_deadline_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    catalog = FakeCatalog(_job(deadline_at=datetime.now(UTC) + timedelta(hours=1)))
    worker, metrics_path = _failing_worker(catalog, tmp_path, monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.SUCCEEDED.value
    assert "jobs_timeouts_total" not in {point["name"] for point in _points(metrics_path)}
