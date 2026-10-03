from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog.repository import IdempotencyConflict, InvalidTransition
from data_engine.config import Settings


class FakeCatalog:
    """In-memory stand-in with the repository's idempotency semantics.

    Key reuse replays only an *identical* request; a different request under the
    same key is a conflict (F4). The identity mirror is kept beside the job map
    rather than inside the job row, because the row is also the response body.
    """

    def __init__(self) -> None:
        self.jobs_by_key: dict[str, dict[str, Any]] = {}
        self.jobs_by_id: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, tuple[Any, ...]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.last_payload: dict[str, Any] = {}

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
        self.last_payload = payload
        identity = (job_type, payload, max_attempts, deadline_seconds)
        if key and key in self.jobs_by_key:
            if self.identities.get(key) != identity:
                raise IdempotencyConflict(key)
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
            self.identities[key] = identity
        return job, True

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs_by_id.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] != "queued":
            raise InvalidTransition(f"{job['state']} -> canceled")
        job["state"] = "canceled"
        return job

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs_by_id.get(job_id)


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
def test_replaying_an_identical_submission_reuses_the_job_and_says_so() -> None:
    """F4 at the HTTP edge: the second POST must not queue a second job."""
    client = _client()
    headers = {"Idempotency-Key": "key-replay"}

    first = client.post("/api/v1/jobs", json=_payload(), headers=headers)
    second = client.post("/api/v1/jobs", json=_payload(), headers=headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.headers["x-idempotent-replay"] == "false"
    assert second.headers["x-idempotent-replay"] == "true"
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.contract
def test_reusing_an_idempotency_key_for_a_different_request_conflicts() -> None:
    """F4: a key identifies one request, so a second one must 409, not replay."""
    client = _client()
    headers = {"Idempotency-Key": "key-conflict"}
    assert client.post("/api/v1/jobs", json=_payload(), headers=headers).status_code == 202

    other = _payload()
    other["payload"]["episode"]["task"] = "a_different_task"
    response = client.post("/api/v1/jobs", json=other, headers=headers)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert body["correlation_id"]


class DatabaseDownCatalog(FakeCatalog):
    """The catalog as it answers while Postgres is unreachable (F10)."""

    def submit_job(self, *_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
        raise psycopg.OperationalError("connection refused: server closed the connection")


@pytest.mark.contract
def test_a_database_outage_is_a_retryable_503_not_a_500() -> None:
    """A database that is down is a condition to retry, not an internal bug."""
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = DatabaseDownCatalog()
    client = TestClient(app)

    response = client.post("/api/v1/jobs", json=_payload())

    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "5"
    assert response.headers["x-correlation-id"]
    body = response.json()
    assert body["code"] == "SERVICE_UNAVAILABLE"
    assert body["status"] == 503
    assert body["correlation_id"] == response.headers["x-correlation-id"]


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
def test_source_ingest_submits_a_path_rather_than_a_body() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = FakeCatalog()
    app.state.catalog = catalog
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        json={
            "type": "ingest_source",
            "payload": {"source": "C:/data/so101", "episode_key": "episode_index=7"},
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["type"] == "ingest_source"
    assert catalog.last_payload == {
        "source": "C:/data/so101",
        "episode_key": "episode_index=7",
    }


@pytest.mark.contract
def test_source_ingest_rejects_a_payload_that_names_nothing() -> None:
    """`either a body or a path` must be two types, not one optional field."""
    client = _client()
    assert (
        client.post("/api/v1/jobs", json={"type": "ingest_source", "payload": {}}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/jobs", json={"type": "ingest_source", "payload": {"episode": {}}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/jobs",
            json={"type": "ingest", "payload": {"source": "C:/data"}},
        ).status_code
        == 422
    )


@pytest.mark.contract
def test_source_ingest_carries_the_same_retry_policy() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = FakeCatalog()
    app.state.catalog = catalog
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        json={
            "type": "ingest_source",
            "payload": {"source": "C:/data/so101"},
            "max_attempts": 1,
            "deadline_seconds": 120,
        },
    )

    assert response.status_code == 202, response.text
    assert catalog.submissions == [{"max_attempts": 1, "deadline_seconds": 120.0}]


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


def test_export_job_submits_with_its_own_contract() -> None:
    """`export` names a build; the payload is flat, like validate/build."""
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = FakeCatalog()
    app.state.catalog = catalog
    client = TestClient(app)

    response = client.post(
        "/api/v1/jobs",
        json={"type": "export", "payload": {"build_hash": "bld_abc123"}},
        headers={"Idempotency-Key": "exp-1"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["type"] == "export"
    assert body["payload"] == {"build_hash": "bld_abc123"}
    assert catalog.last_payload == {"build_hash": "bld_abc123"}


def test_export_job_rejects_an_empty_build_hash() -> None:
    response = _client().post(
        "/api/v1/jobs", json={"type": "export", "payload": {"build_hash": ""}}
    )
    assert response.status_code == 422


def test_export_job_rejects_unknown_payload_fields() -> None:
    response = _client().post(
        "/api/v1/jobs",
        json={"type": "export", "payload": {"build_hash": "bld_x", "unexpected": 1}},
    )
    assert response.status_code == 422
