"""Crash recovery: a worker that dies mid-job must not strand the job.

These are the guarantees a stub cannot prove. A worker killed by a container restart
never runs its own failure path, so the job is left in `running`; nothing that runs in
the product used to notice, because the reapers existed but no caller ever invoked
them. Each test drives real SQL so the state transitions are enforced by the database
rather than by the test's own assumptions.

Everything is UUID-scoped, and because `claim_job` takes the oldest queued job in the
database rather than a specific one, each helper drains the queue until it reaches its
own job. That also tidies up after earlier tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.jobs.state import ORPHANED_JOB_SECONDS, JobState
from tests.conftest import postgres_test_dsn


@pytest.fixture
def catalog() -> Iterator[PostgresCatalog]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    settings = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(settings)
    yield PostgresCatalog(settings)


def _abandoned(
    catalog: PostgresCatalog,
    *,
    age_seconds: float,
    max_attempts: int = 3,
) -> str:
    """Submit and claim a job, then backdate `started_at` to fake a dead worker.

    Returns the job id.
    """
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job(
        "ingest", {}, f"orphan-{token}", "test-correlation", max_attempts=max_attempts
    )
    job_id = str(job["id"])
    for _ in range(200):
        claimed = catalog.claim_job()
        if claimed is not None and claimed["id"] == job_id:
            break
    else:
        pytest.fail("could not reach the job to abandon it")
    with connect(catalog.settings) as connection:
        connection.execute(
            "UPDATE jobs SET started_at = now() - make_interval(secs => %s) WHERE id = %s",
            (age_seconds, job_id),
        )
    return job_id


@pytest.mark.integration
def test_a_dead_workers_job_is_requeued_not_stranded(catalog: PostgresCatalog) -> None:
    """The core gap: this job used to stay `running` forever."""
    job_id = _abandoned(catalog, age_seconds=ORPHANED_JOB_SECONDS + 60)

    catalog.reap_orphaned_jobs()

    reclaimed = catalog.get_job(job_id)
    assert reclaimed is not None
    assert reclaimed["state"] == JobState.QUEUED.value
    # Requeued, not failed: the retry budget is still there for whoever picks it up.
    assert int(reclaimed["attempts"]) < int(reclaimed["max_attempts"])
    error: dict[str, Any] = reclaimed["error"]  # type: ignore[assignment]
    assert error["type"] == "WorkerLost"


@pytest.mark.integration
def test_a_requeued_orphan_is_actually_claimable_again(catalog: PostgresCatalog) -> None:
    """Recovery is only real if the normal claim path can pick the job back up."""
    job_id = _abandoned(catalog, age_seconds=ORPHANED_JOB_SECONDS + 60)
    catalog.reap_orphaned_jobs()

    for _ in range(200):
        claimed = catalog.claim_job()
        if claimed is not None and claimed["id"] == job_id:
            break
    else:
        pytest.fail("reclaimed orphan was never claimable again")

    assert claimed["state"] == JobState.RUNNING.value


@pytest.mark.integration
def test_a_healthy_long_running_job_is_left_alone(catalog: PostgresCatalog) -> None:
    """A false reclaim duplicates work, so the window must not be tight."""
    job_id = _abandoned(catalog, age_seconds=ORPHANED_JOB_SECONDS - 300)

    catalog.reap_orphaned_jobs()

    untouched = catalog.get_job(job_id)
    assert untouched is not None
    assert untouched["state"] == JobState.RUNNING.value


@pytest.mark.integration
def test_orphan_without_retry_budget_fails_rather_than_stranding(
    catalog: PostgresCatalog,
) -> None:
    """With no attempts left it can never be claimed, so `running` would be permanent."""
    job_id = _abandoned(catalog, age_seconds=ORPHANED_JOB_SECONDS + 60, max_attempts=1)

    catalog.reap_orphaned_jobs()

    settled = catalog.get_job(job_id)
    assert settled is not None
    assert settled["state"] == JobState.FAILED.value
