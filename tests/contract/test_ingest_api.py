from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog.repository import InvalidTransition
from data_engine.config import Settings


class FakeCatalog:
    def __init__(self) -> None:
        self.jobs_by_key: dict[str, dict[str, Any]] = {}
        self.jobs_by_id: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []

    def submit_job(
        self,
        job_type: str,
        payload: dict[str, Any],
        key: str | None,
        correlation_id: str,
        *,
        max_attempts: int = 3,
        deadline_seconds: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self.submissions.append(
            {"max_attempts": max_attempts, "deadline_seconds": deadline_seconds}
        )
        if key and key in self.jobs_by_key:
            return self.jobs_by_key[key], False
        job = {
            "id": f"job-{len(self.jobs_by_id) + 1}",
            "type": job_type,
            "state": "queued",
            "payload": payload,
            "result": None,
            "error": None,
            "correlation_id": correlation_id,
            "attempts": 0,
            "max_attempts": max_attempts,
            "deadline_at": "2026-09-28T01:00:00Z" if deadline_seconds else None,
            "created_at": "2026-09-28T00:00:00Z",
            "started_at": None,
            "finished_at": None,
        }
        self.jobs_by_id[job["id"]] = job
        if key:
            self.jobs_by_key[key] = job
        return job, True

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs_by_id.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] != "queued":
            raise InvalidTransition(f"{job['state']} -> canceled")
        job["state"] = "canceled"
        return job

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


@pytest.mark.contract
def test_submission_carries_the_retry_budget_and_deadline() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = FakeCatalog()
    app.state.catalog = catalog
    client = TestClient(app)

    body = _payload() | {"max_attempts": 2, "deadline_seconds": 300}
    response = client.post("/api/v1/jobs", json=body)

    assert response.status_code == 202, response.text
    assert catalog.submissions == [{"max_attempts": 2, "deadline_seconds": 300.0}]
    assert response.json()["max_attempts"] == 2
    assert response.json()["deadline_at"] == "2026-09-28T01:00:00Z"


@pytest.mark.contract
@pytest.mark.parametrize("field", ["max_attempts", "deadline_seconds"])
def test_submission_rejects_an_out_of_range_lifecycle_policy(field: str) -> None:
    body = _payload() | {field: 0}
    assert _client().post("/api/v1/jobs", json=body).status_code == 422


@pytest.mark.contract
def test_submission_rejects_unknown_policy_fields() -> None:
    body = _payload() | {"retries": 3}
    assert _client().post("/api/v1/jobs", json=body).status_code == 422


@pytest.mark.contract
def test_cancel_endpoint_settles_a_queued_job() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = FakeCatalog()
    client = TestClient(app)
    job_id = client.post("/api/v1/jobs", json=_payload()).json()["id"]

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "canceled"


@pytest.mark.contract
def test_cancel_endpoint_reports_missing_and_conflicting_jobs() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = FakeCatalog()
    app.state.catalog = catalog
    client = TestClient(app)
    job = catalog.submit_job("ingest", {"episode": {}}, None, "corr-1")[0]

    assert client.post("/api/v1/jobs/missing/cancel").status_code == 404

    catalog.jobs_by_id[job["id"]]["state"] = "running"
    conflict = client.post(f"/api/v1/jobs/{job['id']}/cancel")
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "INVALID_TRANSITION"
    assert "running -> canceled" in body["detail"]
