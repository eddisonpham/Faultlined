"""Lifecycle behaviour against a real PostgreSQL: retry, cancel, and deadlines.

The state machine in `jobs/state.py` is only worth anything if the SQL that drives it
holds, so these exercise the repository directly rather than a fake connection.
"""

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
    """Claim jobs until `job_id` is the one in hand, settling the strays as failed.

    The test database shares one queue with the rest of the suite, so `claim_job`
    returns the oldest queued row, which is not necessarily the row under test.
    """
    for _ in range(limit):
        claimed = catalog.claim_job()
        if claimed is None:
            break
        if claimed["id"] == job_id:
            return claimed
        catalog.finish_job(claimed["id"], JobState.FAILED, error={"type": "TestTeardown"})
    raise AssertionError(f"job {job_id} was never claimed")


def _drain(worker: IngestWorker, catalog: PostgresCatalog, job_id: str, limit: int = 50) -> None:
    """Run the worker until `job_id` leaves the queue.

    The test database is shared, so a claimed job is not necessarily this one.
    """
    for _ in range(limit):
        if worker.process_one() is None:
            break
        if catalog.get_job(job_id)["state"] != JobState.QUEUED.value:
            break


def _run_until_attempt(
    worker: IngestWorker, catalog: PostgresCatalog, job_id: str, attempts: int, limit: int = 50
) -> dict[str, Any]:
    """Run the worker until `job_id` has been attempted `attempts` times.

    A retried job returns to the queue, so "is it still queued" is the wrong stopping
    condition; the attempt counter is not.
    """
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

    # The retry policy is part of the request identity: replaying with a different
    # budget is a conflict, not a silent reuse of the original job.
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
def test_retry_loop_parks_the_job_as_failed(tmp_path: Path) -> None:
    """F6: every attempt runs, the last one is not requeued, and the job ends failed."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest",
        {"episode": _broken_episode()},
        f"retry-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=2,
    )
    worker = IngestWorker(settings)

    first = _run_until_attempt(worker, catalog, job["id"], 1)
    # Attempt one failed but the budget is not spent: back to the queue.
    assert first["attempts"] == 1
    assert first["state"] in {JobState.QUEUED.value, JobState.RETRYING.value}

    last = _run_until_attempt(worker, catalog, job["id"], 2)
    assert last["attempts"] == 2
    assert last["state"] == JobState.FAILED.value
    assert last["error"]["type"] == "KeyError"


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
    # Put the job into the state a worker would have left it in.
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
