import time
from datetime import UTC, datetime, timedelta
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
        self._state = "queued" if job else None
        self.queued = 1 if job else 0
        self.cancel_requested = False
        self.requeues = 0

    def count_jobs(self, state: JobState) -> int:
        return self.queued

    def claim_job(self) -> dict[str, Any] | None:
        if self.job is None:
            return None
        self._state = "running"
        if self.job is not None:
            self.job["attempts"] = int(self.job.get("attempts", 0)) + 1
        return self.job

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.cancel_requested

    def requeue_for_retry(self, job_id: str) -> dict[str, Any]:
        self._state = JobState.QUEUED.value
        self.requeues += 1
        return {"id": job_id, "state": self._state}

    def mark_deadline_expired(self, job_id: str) -> dict[str, Any]:
        self._state = JobState.TIMED_OUT.value
        self.error = {"type": "DeadlineExceeded", "message": "job exceeded its deadline"}
        return {"id": job_id, "state": self._state}

    def get_job(self, job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"id": job_id, "state": self._state}
        if hasattr(self, "error"):
            result["error"] = self.error
        return result

    def finish_job(self, job_id: str, state: JobState, **kwargs: Any) -> dict[str, Any]:
        self._state = state.value
        if kwargs.get("error") is not None:
            self.error = kwargs["error"]
        return {"id": job_id, "state": state.value, **kwargs}


def _job(
    *,
    attempts: int = 1,
    max_attempts: int = 1,
    deadline_at: Any = None,
) -> dict[str, Any]:
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
            "episode": {
                "task": "pick",
                "robot": "test-arm",
                "timestamps": [0.0, 0.1],
                "observations": [[0.0], [1.0]],
                "actions": [[0.1], [0.2]],
            }
        },
        "deadline_at": deadline_at,
    }


def _worker(
    catalog: FakeCatalog,
    tmp_path: Path,
    *,
    metrics_payload: bool = True,
    handler: Any = None,
) -> IngestWorker:
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    metrics = RuntimeMetrics(JsonlMetricSink(metrics_path)) if metrics_payload else None
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, _env_file=None),
        metrics=metrics,
        catalog=catalog,
    )
    if handler is not None:
        worker._run_handler = handler  # type: ignore[attr-defined]
    return worker


def _points(path: Path) -> list[dict[str, Any]]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.unit
def test_worker_times_out_a_job_whose_deadline_already_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(
        _job(attempts=0, max_attempts=3, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    worker = _worker(catalog, tmp_path)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.TIMED_OUT.value
    assert result["error"]["type"] == "DeadlineExceeded"
    # A job that never ran must not be recorded as a handler failure.
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_failures_total" not in {point["name"] for point in points}
    assert "jobs_timeouts_total" in {point["name"] for point in points}


@pytest.mark.unit
def test_worker_times_out_a_handler_that_overruns_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: a handler that starts before the deadline and overruns it is time out.

    The deadline watchdog re-checks at every checkpoint, so an overrunning
    handler is killed even though it started on time.
    """
    deadline_at = datetime.now(UTC) + timedelta(seconds=5)
    catalog = FakeCatalog(_job(deadline_at=deadline_at, attempts=1, max_attempts=1))
    worker = _worker(catalog, tmp_path)

    # The handler starts near the end of the budget: it lasts 6 s of a 5 s
    # budget, so the 5-second deadline is exceeded before the handler returns.
    def _slow_handler(self, job: dict[str, Any], job_type: str) -> dict[str, Any]:
        time.sleep(6)
        return {"episode_id": "ep-1"}

    monkeypatch.setattr("data_engine.jobs.worker.IngestWorker._run_handler", _slow_handler)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.TIMED_OUT.value
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_timeouts_total" in {point["name"] for point in points}
    assert "jobs_failures_total" not in {point["name"] for point in points}


@pytest.mark.unit
def test_worker_leaves_a_future_deadline_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job(deadline_at=datetime.now(UTC) + timedelta(hours=1)))

    def _noop_handler(job: dict[str, Any], job_type: str) -> dict[str, Any]:
        return {"episode_id": "ep-1"}

    worker = _worker(catalog, tmp_path, handler=_noop_handler)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.SUCCEEDED.value
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_timeouts_total" not in {point["name"] for point in points}
    assert "jobs_failures_total" not in {point["name"] for point in points}


@pytest.mark.unit
def test_worker_cancels_a_running_job_at_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: a cancel requested while the handler runs is delivered at the checkpoint."""
    catalog = FakeCatalog(_job(deadline_at=datetime.now(UTC) + timedelta(hours=1)))
    catalog.cancel_requested = True
    worker = _worker(catalog, tmp_path)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.CANCELED.value
    # A coordinator cancel is not a retry: the job must not be requeued.
    assert catalog.requeues == 0
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_cancellations_total" in {point["name"] for point in points}


@pytest.mark.unit
def test_worker_ignores_a_cancel_after_the_handler_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel is honored at the next checkpoint, not retroactive."""
    catalog = FakeCatalog(_job(deadline_at=datetime.now(UTC) + timedelta(hours=1)))

    def _noop_handler(job: dict[str, Any], job_type: str) -> dict[str, Any]:
        return {"episode_id": "ep-1"}

    worker = _worker(catalog, tmp_path, handler=_noop_handler)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.SUCCEEDED.value
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_cancellations_total" not in {point["name"] for point in points}


@pytest.mark.unit
def test_worker_cancel_beats_a_retry_on_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F13-adjacent: a cancel request survives a transient failure and wins over retry."""
    catalog = FakeCatalog(
        _job(
            attempts=1,
            max_attempts=3,
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    catalog.cancel_requested = True

    # Make every handler invocation fail transiently so the worker walks the
    # retry branch; on each walk the cancel check in _settle_failure must fire
    # and win over requeuing.
    monkeypatch.setattr(
        "data_engine.jobs.worker.IngestWorker._run_handler",
        lambda _: (_ for _ in ()).throw(RuntimeError("ingest service down")),
    )

    worker = _worker(catalog, tmp_path)
    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.CANCELED.value
    # Every transient failure was swallowed by a cancel, never requeued.
    assert catalog.requeues == 0
    points = _points(tmp_path / "metrics" / "runtime.jsonl")
    assert "jobs_cancellations_total" in {point["name"] for point in points}
    # No retry fires: cancel was set on the catalog, so _settle_failure
    # settles as canceled before the retry branch is reached.
