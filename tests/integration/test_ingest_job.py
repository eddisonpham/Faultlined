import uuid
from pathlib import Path
from typing import Any

import pytest

from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import IdempotencyConflict, PostgresCatalog
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker
from tests.conftest import postgres_test_dsn

pytestmark = [pytest.mark.integration]


def _settings() -> Settings:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration tests")
    return Settings(database_url=dsn, artifact_root=Path("var/test-artifacts"), _env_file=None)


def _episode() -> dict[str, Any]:
    return {
        "task": "integration_task",
        "robot": "synthetic_arm",
        "timestamps": [0.0, 0.1],
        "observations": [[0.0], [1.0]],
        "actions": [[0.1], [0.2]],
    }


def _drain(worker: IngestWorker, catalog: PostgresCatalog, *job_ids: str) -> None:
    """Run queued jobs until every id given has left the queue.

    `claim_job` is FIFO across the whole shared catalog, so these jobs are only
    reached once every older claimable job has been processed. The loop therefore
    ends when the jobs we care about are done, or when nothing is claimable at
    all -- and it cannot spin forever, because a job that keeps failing spends its
    own attempt budget and stops being claimable.

    This used to be a fixed 50 iterations. That is a flake with a long fuse: every
    other test shares this queue, and the catalog is never truncated between runs,
    so the job under test is only reached after however many jobs happen to be
    queued ahead of it. It failed once the backlog passed 50, passed in isolation,
    and passed on a rerun of the same directory.
    """
    while any(catalog.get_job(job_id)["state"] == JobState.QUEUED.value for job_id in job_ids):
        if worker.process_one() is None:
            return


@pytest.mark.integration
def test_postgres_job_idempotency_and_conflict() -> None:
    settings = _settings()
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)
    key = f"test-{uuid.uuid4()}"
    first, created = catalog.submit_job("ingest", {"episode": _episode()}, key, "test-correlation")
    replay, replay_created = catalog.submit_job(
        "ingest", {"episode": _episode()}, key, "test-correlation"
    )

    assert created is True
    assert replay_created is False
    assert first["id"] == replay["id"]
    with pytest.raises(IdempotencyConflict):
        catalog.submit_job("ingest", {"episode": {"task": "different"}}, key, "test-correlation")
    catalog.finish_job(first["id"], JobState.CANCELED)


@pytest.mark.integration
def test_worker_processes_ingest_and_registers_episode(tmp_path: Path) -> None:
    settings = _settings().model_copy(update={"artifact_root": tmp_path})
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)
    job, _ = catalog.submit_job(
        "ingest", {"episode": _episode()}, f"worker-{uuid.uuid4()}", "test-correlation"
    )

    worker = IngestWorker(settings)
    _drain(worker, catalog, job["id"])

    actual = catalog.get_job(job["id"])
    assert actual is not None
    assert actual["state"] == JobState.SUCCEEDED.value
    assert actual["result"]["episode_id"]
    episode = catalog.get_episode(actual["result"]["episode_id"])
    assert episode is not None
    assert episode["artifact_hash"] == actual["result"]["artifact_hash"]
    # The episode is content-addressed, so a persistent database accumulates one edge per
    # job that produced it. Assert membership, not position, or this fails on the second run.
    assert job["id"] in [edge["to_ref"] for edge in episode["lineage"]]


@pytest.mark.integration
def test_list_jobs_filters_paginates_and_counts() -> None:
    settings = _settings()
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)

    first_id = f"list-{uuid.uuid4()}"
    catalog.submit_job("ingest", {"episode": _episode()}, first_id, "test-correlation")
    catalog.submit_job(
        "ingest", {"episode": _episode()}, f"list-{uuid.uuid4()}", "test-correlation"
    )

    newest_first = catalog.list_jobs(limit=50)
    assert len(newest_first) >= 2
    assert [r["id"] for r in newest_first][:2] == sorted(
        [r["id"] for r in newest_first][:2],
        key=lambda i: catalog.get_job(i)["created_at"],
        reverse=True,
    )

    # Cursor paging must not repeat a row already returned.
    first_page = catalog.list_jobs(limit=1)
    assert len(first_page) == 1
    second_page = catalog.list_jobs(limit=1, before=first_page[0]["created_at"])
    assert second_page and second_page[0]["id"] != first_page[0]["id"]

    queued = catalog.list_jobs(state=JobState.QUEUED, limit=200)
    assert all(r["state"] == JobState.QUEUED.value for r in queued)
    assert catalog.count_jobs(JobState.QUEUED) >= len(queued)
    assert catalog.count_artifacts() >= 0
    assert catalog.count_episodes() >= 0


@pytest.mark.integration
def test_list_artifacts_reports_referencing_episodes(tmp_path: Path) -> None:
    settings = _settings().model_copy(update={"artifact_root": tmp_path})
    initialize_schema(settings)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)

    submitted = []
    for _ in range(3):
        job, _ = catalog.submit_job(
            "ingest", {"episode": _episode()}, f"artifacts-{uuid.uuid4()}", "test-correlation"
        )
        submitted.append(job["id"])
    _drain(worker, catalog, *submitted)

    artifacts = catalog.list_artifacts(limit=200)
    assert artifacts, "the ingested episode should have written an artifact"
    row = artifacts[0]
    assert row["size_bytes"] > 0
    assert isinstance(row["episode_ids"], list)
    # The synthetic episode is content-addressed, so every run maps to the same blob.
    assert row["episode_ids"], "an ingested artifact should reference at least one episode"
