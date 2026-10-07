"""Lifecycle behaviour against a real PostgreSQL: retry, cancel, and deadlines."""

import errno
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.repository import (
    IdempotencyConflict,
    InvalidTransition,
    PostgresCatalog,
)
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics
from data_engine.storage.artifacts import FileArtifactStore
from tests.conftest import postgres_test_dsn

pytestmark = [pytest.mark.integration]


def _settings(tmp_path: Path) -> Settings:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration tests")
    settings = Settings(database_url=dsn, artifact_root=tmp_path, _env_file=None)
    initialize_schema(settings)
    return settings


def _broken_episode() -> dict[str, Any]:
    """A payload the ingest service rejects, so the job keeps failing."""
    return {"task": "missing-fields"}


def _claim_until(catalog: PostgresCatalog, job_id: str, limit: int = 50) -> dict[str, Any]:
    """Claim jobs until `job_id` is the one in hand, settling the strays as failed."""
    for _ in range(limit):
        claimed = catalog.claim_job()
        if claimed is None:
            break
        if claimed["id"] == job_id:
            return claimed
        catalog.finish_job(claimed["id"], JobState.FAILED, error={"type": "TestTeardown"})
    raise AssertionError(f"job {job_id} was never claimed")


def _drain(worker: IngestWorker, catalog: PostgresCatalog, job_id: str, limit: int = 50) -> None:
    """Run the worker until `job_id` leaves the queue."""
    for _ in range(limit):
        if worker.process_one() is None:
            break
        if catalog.get_job(job_id)["state"] != JobState.QUEUED.value:
            break


def _run_until_attempt(
    worker: IngestWorker, catalog: PostgresCatalog, job_id: str, attempts: int, limit: int = 50
) -> dict[str, Any]:
    """Run the worker until `job_id` has been attempted `attempts` times."""
    for _ in range(limit):
        if int(catalog.get_job(job_id)["attempts"]) >= attempts:
            return catalog.get_job(job_id)
        if worker.process_one() is None:
            break
    return catalog.get_job(job_id)


@pytest.mark.integration
def test_submit_job_records_retry_budget_and_deadline(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    key = f"lifecycle-{uuid.uuid4()}"

    job, created = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        key,
        "test-correlation",
        max_attempts=2,
        deadline_seconds=600,
    )

    assert created is True
    assert job["attempts"] == 0
    assert job["max_attempts"] == 2
    assert job["deadline_at"] is not None
    assert job["deadline_at"] > job["created_at"]

    with pytest.raises(IdempotencyConflict):
        catalog.submit_job(
            "ingest", {"episode": _broken_episode()}, key, "test-correlation", max_attempts=5
        )
    catalog.request_cancel(job["id"])


@pytest.mark.integration
def test_claim_job_counts_attempts_and_stops_when_exhausted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest", {"episode": _broken_episode()}, f"attempts-{uuid.uuid4()}", "test-correlation"
    )
    assert job["max_attempts"] > 0

    claimed = _claim_until(catalog, job["id"])

    assert claimed["attempts"] == 1
    assert claimed["state"] == JobState.RUNNING.value
    catalog.finish_job(job["id"], JobState.FAILED, error={"type": "TestTeardown"})


@pytest.mark.integration
def test_claim_job_never_returns_a_job_with_spent_attempts(tmp_path: Path) -> None:
    """The `attempts < max_attempts` guard is what stops a retry storm."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"exhausted-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=1,
    )
    with connect(settings) as connection:
        connection.execute(
            "UPDATE jobs SET attempts = max_attempts, state = %s WHERE id = %s",
            (JobState.QUEUED.value, job["id"]),
        )
        connection.commit()

    for _ in range(50):
        claimed = catalog.claim_job()
        if claimed is None:
            break
        assert claimed["id"] != job["id"], "an exhausted job must never be claimed again"
        catalog.finish_job(claimed["id"], JobState.FAILED, error={"type": "TestTeardown"})

    assert catalog.get_job(job["id"])["state"] == JobState.QUEUED.value


@pytest.mark.integration
def test_retry_budget_is_enforced_by_the_repository(tmp_path: Path) -> None:
    """F6 against real SQL: the requeue/claim cycle spends the budget exactly once."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"retry-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=2,
    )

    claimed = _claim_until(catalog, job["id"])
    assert claimed["attempts"] == 1
    catalog.finish_job(job["id"], JobState.RETRYING, error={"type": "ConnectionResetError"})
    requeued = catalog.requeue_for_retry(job["id"])
    assert requeued["state"] == JobState.QUEUED.value
    assert requeued["error"] is None, "a fresh attempt must not inherit the old error"

    second = _claim_until(catalog, job["id"])
    assert second["attempts"] == 2
    catalog.finish_job(job["id"], JobState.RETRYING)
    with pytest.raises(ValueError, match="attempts exhausted"):
        catalog.requeue_for_retry(job["id"])
    catalog.finish_job(job["id"], JobState.FAILED, error={"type": "ConnectionResetError"})

    final = catalog.get_job(job["id"])
    assert final["attempts"] == 2
    assert final["state"] == JobState.FAILED.value


@pytest.mark.integration
def test_cancel_stops_a_queued_job_outright(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"cancel-queued-{uuid.uuid4()}",
        "test-correlation",
    )

    canceled = catalog.request_cancel(job["id"])

    assert canceled["state"] == JobState.CANCELED.value
    assert catalog.get_job(job["id"])["state"] == JobState.CANCELED.value


@pytest.mark.integration
def test_cancel_marks_a_running_job_and_the_worker_settles_it(tmp_path: Path) -> None:
    """F7: a running job is flagged, and the worker's next failure cancels instead of retrying."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"cancel-running-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=5,
    )
    with connect(settings) as connection:
        connection.execute(
            "UPDATE jobs SET state = %s WHERE id = %s", (JobState.RUNNING.value, job["id"])
        )
        connection.commit()

    flagged = catalog.request_cancel(job["id"])
    assert flagged["state"] == JobState.CANCEL_REQUESTED.value
    assert catalog.is_cancel_requested(job["id"]) is True

    settled = catalog.finish_job(job["id"], JobState.CANCELED, error={"type": "Canceled"})
    assert settled["state"] == JobState.CANCELED.value
    assert catalog.is_cancel_requested(job["id"]) is False


@pytest.mark.integration
def test_terminal_states_reject_further_transitions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest", {"episode": _broken_episode()}, f"terminal-{uuid.uuid4()}", "test-correlation"
    )
    catalog.request_cancel(job["id"])

    with pytest.raises(InvalidTransition):
        catalog.finish_job(job["id"], JobState.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        catalog.request_cancel(job["id"])


@pytest.mark.integration
def test_reaper_times_out_expired_deadlines_only(tmp_path: Path) -> None:
    """F6: the reaper sweeps expired deadlines and leaves healthy work alone."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    expired, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"deadline-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=5,
        deadline_seconds=-1,
    )
    healthy, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"deadline-ok-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=5,
        deadline_seconds=3600,
    )

    reaped = catalog.reap_expired_deadlines()

    assert reaped >= 1
    timed_out = catalog.get_job(expired["id"])
    assert timed_out["state"] == JobState.TIMED_OUT.value
    assert timed_out["error"]["type"] == "DeadlineExceeded"
    assert catalog.get_job(healthy["id"])["state"] == JobState.QUEUED.value


@pytest.mark.integration
def test_worker_times_out_a_job_whose_deadline_passed_before_it_ran(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"worker-deadline-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=5,
    )
    with connect(settings) as connection:
        connection.execute(
            "UPDATE jobs SET deadline_at = %s WHERE id = %s",
            (job["created_at"] - timedelta(seconds=1), job["id"]),
        )
        connection.commit()

    worker = IngestWorker(settings)
    _drain(worker, catalog, job["id"])

    timed_out = catalog.get_job(job["id"])
    assert timed_out["state"] == JobState.TIMED_OUT.value
    assert timed_out["attempts"] == 1


@pytest.mark.integration
def test_an_artifact_write_failure_fails_the_job_and_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9: a store that cannot write leaves a failed job, not a dangling row."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {
            "episode": {
                "task": "pick_place",
                "robot": "test_arm",
                "timestamps": [0.0, 0.1],
                "observations": [[0.0], [1.0]],
                "actions": [[0.1], [0.2]],
            }
        },
        f"diskfull-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=1,
    )
    with connect(settings) as connection:
        connection.execute(
            "UPDATE jobs SET created_at = now() - interval '1 day' WHERE id = %s",
            (job["id"],),
        )
        connection.commit()

    def _full_disk(_self: FileArtifactStore, _data: bytes) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(FileArtifactStore, "put_bytes", _full_disk)
    metrics_path = tmp_path / "metrics" / "runtime.jsonl"
    worker = IngestWorker(settings, metrics=RuntimeMetrics(JsonlMetricSink(metrics_path)))

    _drain(worker, catalog, job["id"])

    final = catalog.get_job(job["id"])
    assert final["state"] == JobState.FAILED.value
    assert final["error"]["type"] == "OSError"
    assert catalog.episodes_produced_by(str(job["id"])) == []
    points = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
    failures = [point for point in points if point["name"] == "jobs_failures_total"]
    assert failures[0]["labels"]["reason_code"] == "IO_DISK_FULL"
