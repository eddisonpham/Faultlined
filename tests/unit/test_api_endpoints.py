from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings


class CatalogStub:
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if job_id == "job-1":
            return {
                "id": "job-1",
                "type": "ingest",
                "state": "succeeded",
                "payload": {},
                "result": {"episode_id": "episode-1"},
                "error": None,
                "correlation_id": "corr-1",
                "created_at": "2026-09-28T00:00:00Z",
                "started_at": None,
                "finished_at": None,
            }
        return None

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        if episode_id == "episode-1":
            return {
                "id": episode_id,
                "source_hash": "a" * 64,
                "artifact_hash": "a" * 64,
                "format": "synthetic-json",
                "metadata": {"task": "pick"},
                "lineage": [
                    {
                        "from_type": "episode",
                        "from_ref": episode_id,
                        "to_type": "job",
                        "to_ref": "job-1",
                        "relation": "produced_by",
                    }
                ],
                "created_at": "2026-09-28T00:00:00Z",
            }
        return None


def _client() -> TestClient:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = CatalogStub()
    return TestClient(app)


@pytest.mark.contract
def test_health_route_returns_ok_without_database() -> None:
    assert _client().get("/api/v1/health").json() == {"status": "ok"}


@pytest.mark.contract
def test_root_points_a_first_visitor_somewhere_useful() -> None:
    """The platform has no web UI yet, so `/` must not be a blank 404."""
    body = _client().get("/").json()
    assert body["service"] == "faultlined"
    assert body["docs"] == "/docs"
    assert body["endpoints"]["submit_job"] == "POST /api/v1/jobs"


@pytest.mark.contract
def test_get_job_and_episode_resources() -> None:
    client = _client()
    job = client.get("/api/v1/jobs/job-1")
    episode = client.get("/api/v1/episodes/episode-1")
    assert job.status_code == 200
    assert job.json()["result"]["episode_id"] == "episode-1"
    assert episode.status_code == 200
    assert episode.json()["format"] == "synthetic-json"
    assert episode.json()["lineage"][0]["to_ref"] == "job-1"


@pytest.mark.contract
def test_correlation_id_is_generated_or_forwarded() -> None:
    client = _client()
    generated = client.get("/api/v1/health")
    forwarded = client.get("/api/v1/health", headers={"X-Correlation-Id": "known-id"})
    assert generated.headers["x-correlation-id"]
    assert forwarded.headers["x-correlation-id"] == "known-id"
