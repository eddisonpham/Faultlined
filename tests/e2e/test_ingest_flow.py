import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker


class InMemoryCatalog:
    """Tiny E2E adapter preserving API/worker/artifact boundaries without external services."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.episodes: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}

    def submit_job(
        self, job_type: str, payload: dict[str, Any], key: str | None, correlation_id: str
    ) -> tuple[dict[str, Any], bool]:
        if key and key in self.keys:
            return self.jobs[self.keys[key]], False
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "type": job_type,
            "state": "queued",
            "payload": payload,
            "result": None,
            "error": None,
            "correlation_id": correlation_id,
            "created_at": "2026-09-28T00:00:00Z",
            "started_at": None,
            "finished_at": None,
        }
        self.jobs[job_id] = job
        if key:
            self.keys[key] = job_id
        return job, True

    def count_jobs(self, state: str) -> int:
        return sum(1 for job in self.jobs.values() if job["state"] == state)

    def claim_job(self) -> dict[str, Any] | None:
        job = next((item for item in self.jobs.values() if item["state"] == "queued"), None)
        if job is None:
            return None
        job["state"] = "running"
        job["started_at"] = "2026-09-28T00:00:01Z"
        return job

    def register_episode(self, **kwargs: Any) -> dict[str, Any]:
        episode_id = str(uuid.uuid4())
        episode = {
            "id": episode_id,
            "source_hash": kwargs["source_hash"],
            "artifact_hash": kwargs["artifact_hash"],
            "format": "synthetic-json",
            "metadata": kwargs["metadata"],
            "created_at": "2026-09-28T00:00:00Z",
            "lineage": [
                {
                    "from_type": "episode",
                    "from_ref": episode_id,
                    "to_type": "job",
                    "to_ref": kwargs["job_id"],
                    "relation": "produced_by",
                }
            ],
        }
        self.episodes[episode_id] = episode
        return episode

    def finish_job(
        self,
        job_id: str,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self.jobs[job_id]
        job["state"] = state.value
        job["result"] = result
        job["error"] = error
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return self.episodes.get(episode_id)


@pytest.mark.e2e
def test_api_worker_artifact_and_lineage_end_to_end(tmp_path: Path) -> None:
    settings = Settings(artifact_root=tmp_path, _env_file=None)
    catalog = InMemoryCatalog()
    app = create_app(settings, initialize_database=False, catalog=catalog)  # type: ignore[arg-type]
    client = TestClient(app)
    key = f"e2e-{uuid.uuid4()}"

    submitted = client.post(
        "/api/v1/jobs",
        headers={"Idempotency-Key": key, "X-Correlation-Id": "e2e-correlation"},
        json={
            "type": "ingest",
            "payload": {
                "episode": {
                    "task": "e2e_pick_place",
                    "robot": "synthetic_arm",
                    "timestamps": [0.0, 0.1],
                    "observations": [[0.0], [1.0]],
                    "actions": [[0.1], [0.2]],
                }
            },
        },
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["id"]

    worker = IngestWorker(settings)
    worker.catalog = catalog  # type: ignore[assignment]
    worker.ingest.catalog = catalog  # type: ignore[assignment]  # artifact store remains real
    completed = worker.process_one()
    assert completed is not None and completed["id"] == job_id

    job_response = client.get(f"/api/v1/jobs/{job_id}")
    assert job_response.status_code == 200
    assert job_response.json()["state"] == "succeeded"
    episode_id = job_response.json()["result"]["episode_id"]
    episode_response = client.get(f"/api/v1/episodes/{episode_id}")
    assert episode_response.status_code == 200
    assert episode_response.json()["lineage"][0]["to_ref"] == job_id
    assert worker.artifacts.get_bytes(episode_response.json()["artifact_hash"])


@pytest.mark.e2e
@pytest.mark.integration
def test_real_postgres_catalog_initializes_when_configured() -> None:
    dsn = os.environ.get("DE_DATABASE_URL")
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration test")
    settings = Settings(database_url=dsn, _env_file=None)
    initialize_schema(settings)
    assert PostgresCatalog(settings).get_job("missing") is None
