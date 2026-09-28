from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings


class FakeCatalog:
    def __init__(self) -> None:
        self.jobs_by_key: dict[str, dict[str, Any]] = {}
        self.jobs_by_id: dict[str, dict[str, Any]] = {}

    def submit_job(
        self, job_type: str, payload: dict[str, Any], key: str | None, correlation_id: str
    ) -> tuple[dict[str, Any], bool]:
        if key and key in self.jobs_by_key:
            return self.jobs_by_key[key], False
        job = {
            "id": "job-1",
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
        self.jobs_by_id[job["id"]] = job
        if key:
            self.jobs_by_key[key] = job
        return job, True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs_by_id.get(job_id)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return None


def _client() -> TestClient:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = FakeCatalog()
    return TestClient(app)


def _payload() -> dict[str, Any]:
    return {
        "type": "ingest",
        "payload": {
            "episode": {
                "task": "pick_place",
                "robot": "synthetic_arm",
                "timestamps": [0.0, 0.1],
                "observations": [[0.0], [1.0]],
                "actions": [[0.1], [0.2]],
            }
        },
    }


@pytest.mark.contract
def test_ingest_rejects_misaligned_samples_without_database() -> None:
    client = _client()
    payload = _payload()
    payload["payload"]["episode"]["observations"] = [[0.0]]
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422
    assert response.headers["x-correlation-id"]


@pytest.mark.contract
def test_api_exposes_openapi_contract() -> None:
    response = _client().get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/jobs" in response.json()["paths"]


@pytest.mark.contract
def test_job_submission_uses_public_contract_and_correlation_header() -> None:
    response = _client().post(
        "/api/v1/jobs",
        headers={"Idempotency-Key": "key-1", "X-Correlation-Id": "corr-1"},
        json=_payload(),
    )
    assert response.status_code == 202, response.text
    assert response.headers["x-correlation-id"] == "corr-1"
    assert response.headers["x-idempotent-replay"] == "false"
    assert response.json()["state"] == "queued"


@pytest.mark.contract
def test_unknown_job_returns_problem_object() -> None:
    response = _client().get("/api/v1/jobs/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
