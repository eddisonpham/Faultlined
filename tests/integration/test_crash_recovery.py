"""Crash recovery: a worker that dies mid-job must not strand the job.

These are the guarantees a stub cannot prove. A worker killed by a container restart
never runs its own failure path, so the job is left in `running`; nothing that runs in
the product used to notice, because the reapers existed but no caller ever invoked
them. Each test drives real SQL so the state transitions are enforced by the database
rather than by the test's own assumptions.

The test database is shared with the whole suite and is long-lived - it already holds
thousands of jobs - so these tests neither drain the queue to reach their own job nor
leave rows behind. `abandoned_job` creates one job per test and removes it afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
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


@pytest.fixture
def abandoned_job(catalog: PostgresCatalog) -> Iterator[Callable[..., str]]:
    """Create one job per test in the state a dead worker leaves behind, then clean up.

    `created_at` is moved just behind the oldest row in the table so the job sorts
    ahead of everything else queued here, which lets a test prove it is claimable
    again with a single `claim_job()` call. A fixed constant would not do: this
    database is long-lived and keeps whatever earlier runs left behind, so the
    threshold is computed rather than assumed. Draining the queue instead would claim
    and abandon jobs that belong to other tests. Jobs created here are removed
    afterwards, so they cannot accumulate.
    """
    created: list[str] = []

    def _make(*, age_seconds: float, max_attempts: int = 3) -> str:
        token = uuid.uuid4().hex
        job, _ = catalog.submit_job(
            "ingest", {}, f"orphan-{token}", "test-correlation", max_attempts=max_attempts
        )
        job_id = str(job["id"])
        with connect(catalog.settings) as connection:
            connection.execute(
                """UPDATE jobs
                   SET state = %s, attempts = 1, finished_at = NULL,
                       started_at = now() - make_interval(secs => %s),
                       created_at = COALESCE(
                           (SELECT min(created_at) FROM jobs), now()) - interval '1 day'
                   WHERE id = %s""",
                (JobState.RUNNING.value, age_seconds, job_id),
            )
        created.append(job_id)
        return job_id

    yield _make

    if created:
        with connect(catalog.settings) as connection:
            connection.execute("DELETE FROM jobs WHERE id = ANY(%s)", (created,))


@pytest.mark.integration
def test_a_dead_workers_job_is_requeued_not_stranded(
    catalog: PostgresCatalog, abandoned_job: Callable[..., str]
) -> None:
    """The core gap: this job used to stay `running` forever."""
    job_id = abandoned_job(age_seconds=ORPHANED_JOB_SECONDS + 60)

    catalog.reap_orphaned_jobs()

    reclaimed = catalog.get_job(job_id)
    assert reclaimed is not None
    assert reclaimed["state"] == JobState.QUEUED.value
    # Requeued, not failed: the retry budget is still there for whoever picks it up.
    assert int(reclaimed["attempts"]) < int(reclaimed["max_attempts"])
    error: dict[str, Any] = reclaimed["error"]  # type: ignore[assignment]
    assert error["type"] == "WorkerLost"


@pytest.mark.integration
def test_a_requeued_orphan_is_actually_claimable_again(
    catalog: PostgresCatalog, abandoned_job: Callable[..., str]
) -> None:
    """Recovery is only real if the normal claim path can pick the job back up."""
    job_id = abandoned_job(age_seconds=ORPHANED_JOB_SECONDS + 60)
    catalog.reap_orphaned_jobs()

    # Safe to claim straight away: this job sorts ahead of everything else queued.
    claimed = catalog.claim_job()
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["state"] == JobState.RUNNING.value


@pytest.mark.integration
def test_a_healthy_long_running_job_is_left_alone(
    catalog: PostgresCatalog, abandoned_job: Callable[..., str]
) -> None:
    """A false reclaim duplicates work, so the window must not be tight."""
    job_id = abandoned_job(age_seconds=ORPHANED_JOB_SECONDS - 300)

    catalog.reap_orphaned_jobs()

    untouched = catalog.get_job(job_id)
    assert untouched is not None
    assert untouched["state"] == JobState.RUNNING.value


@pytest.mark.integration
def test_orphan_without_retry_budget_fails_rather_than_stranding(
    catalog: PostgresCatalog, abandoned_job: Callable[..., str]
) -> None:
    """With no attempts left it can never be claimed, so `running` would be permanent."""
    job_id = abandoned_job(age_seconds=ORPHANED_JOB_SECONDS + 60, max_attempts=1)

    catalog.reap_orphaned_jobs()

    settled = catalog.get_job(job_id)
    assert settled is not None
    assert settled["state"] == JobState.FAILED.value
