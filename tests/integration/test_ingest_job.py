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

    # process_one claims the oldest queued job in the shared catalog, which is not
    # necessarily this one: other tests and any leftover work sit in the same queue.
    # Drain until this job reaches a terminal state instead of assuming it goes first.
    worker = IngestWorker(settings)
    for _ in range(50):
        if worker.process_one() is None:
            break
        if catalog.get_job(job["id"])["state"] != JobState.QUEUED.value:
            break

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
