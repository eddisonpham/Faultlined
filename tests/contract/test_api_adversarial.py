"""Adversarial contract tests: the API attacked as a hostile client would."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings


class RecordingCatalog:
    """Enough catalog for the read routes, recording what it was handed."""

    def __init__(self) -> None:
        self.limits: dict[str, int] = {}
        self.states: list[str | None] = []

    def list_jobs(
        self,
        *,
        state: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        self.limits["jobs"] = limit
        self.states.append(state)
        return []

    def list_episodes(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
        flag: str | None = None,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        self.limits["episodes"] = limit
        return []

    def list_artifacts(self, *, limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        self.limits["artifacts"] = limit
        return []

    def failing_episodes(
        self, *, limit: int = 50, before: Any = None, reason_code: str | None = None
    ) -> list[dict[str, Any]]:
        self.limits["failing_episodes"] = limit
        return []

    def failure_summary(self) -> dict[str, Any] | None:
        return None

    def list_slices(self, *, limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        self.limits["slices"] = limit
        return []

    def list_builds(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.limits["builds"] = limit
        return []

    def list_contracts(
        self, *, outcome: str | None = None, limit: int = 50, before: Any = None
    ) -> list[dict[str, Any]]:
        self.limits["contracts"] = limit
        return []

    def list_incidents(
        self,
        *,
        severity: str | None = None,
        status: str | None = None,
        label: str | None = None,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        self.limits["incidents"] = limit
        return []

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return None


def _client(catalog: RecordingCatalog | None = None) -> TestClient:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = catalog or RecordingCatalog()
    return TestClient(app)


class TestCorrelationHeader:
    def test_crlf_in_the_correlation_header_is_replaced_not_echoed(self) -> None:
        response = _client().get(
            "/api/v1/health", headers={"X-Correlation-Id": "evil\r\nX-Injected: yes"}
        )
        assert response.status_code == 200
        assert response.headers["x-correlation-id"] != "evil\r\nX-Injected: yes"
        assert "\r" not in response.headers["x-correlation-id"]
        assert "\n" not in response.headers["x-correlation-id"]
        assert "x-injected" not in response.headers

    def test_an_oversized_correlation_header_is_replaced(self) -> None:
        response = _client().get("/api/v1/health", headers={"X-Correlation-Id": "A" * 4096})
        assert response.status_code == 200
        assert len(response.headers["x-correlation-id"]) < 4096

    def test_a_well_formed_correlation_header_is_honored(self) -> None:
        response = _client().get(
            "/api/v1/health", headers={"X-Correlation-Id": "corr-2026.09_01/a+ok"}
        )
        assert response.status_code == 200
        assert response.headers["x-correlation-id"] == "corr-2026.09_01/a+ok"

    def test_control_characters_are_replaced(self) -> None:
        response = _client().get(
            "/api/v1/health", headers={"X-Correlation-Id": "id\x00with\x1b[31mescapes"}
        )
        assert response.headers["x-correlation-id"] == response.headers["x-correlation-id"].replace(
            "\x00", ""
        ).replace("\x1b", "")
        assert "\x00" not in response.headers["x-correlation-id"]
        assert "\x1b" not in response.headers["x-correlation-id"]


class TestPaginationBounds:
    ROUTES: ClassVar[list[str]] = [
        "/api/v1/jobs",
        "/api/v1/artifacts",
        "/api/v1/episodes",
        "/api/v1/failures/episodes",
        "/api/v1/slices",
        "/api/v1/incidents",
        "/api/v1/contracts",
        "/api/v1/builds",
        "/ui/failures",
    ]

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("limit", [-5, 0, 501, 10**9])
    def test_out_of_range_limit_is_a_422(self, route: str, limit: int) -> None:
        response = _client().get(route, params={"limit": limit})
        assert response.status_code == 422, f"{route} accepted limit={limit}"

    @pytest.mark.parametrize("route", ROUTES)
    def test_the_boundary_values_are_accepted(self, route: str) -> None:
        assert _client().get(route, params={"limit": 1}).status_code == 200
        assert _client().get(route, params={"limit": 500}).status_code == 200

    def test_an_oversized_limit_never_reaches_the_catalog(self) -> None:
        """The point of the bound: a huge page must not become a huge query."""
        catalog = RecordingCatalog()
        response = _client(catalog).get("/api/v1/jobs", params={"limit": 10**9})
        assert response.status_code == 422
        assert catalog.limits.get("jobs") is None

    def test_an_in_range_limit_is_passed_through_unchanged(self) -> None:
        catalog = RecordingCatalog()
        response = _client(catalog).get("/api/v1/jobs", params={"limit": 7})
        assert response.status_code == 200
        assert catalog.limits["jobs"] == 7

    def test_the_bound_is_documented_in_openapi(self) -> None:
        """A bound only clients can discover by trial is not a contract."""
        schema = _client().get("/openapi.json").json()
        limit = schema["paths"]["/api/v1/jobs"]["get"]["parameters"]
        bound = next(p for p in limit if p["name"] == "limit")["schema"]
        assert bound["minimum"] == 1
        assert bound["maximum"] == 500


class TestEnumAndPathAbuse:
    def test_unknown_job_state_filter_is_a_422_not_a_query(self) -> None:
        response = _client().get("/api/v1/jobs", params={"state": "queued'; DROP TABLE jobs;--"})
        assert response.status_code == 422

    def test_unknown_episode_flag_is_a_422(self) -> None:
        response = _client().get("/api/v1/episodes", params={"flag": "../../etc/passwd"})
        assert response.status_code == 422

    def test_path_traversal_in_an_identifier_is_a_clean_problem_response(self) -> None:
        response = _client().get("/api/v1/jobs/..%2F..%2Fsecrets")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] and body["title"]

    def test_encoded_unicode_and_nul_identifiers_get_a_clean_problem(self) -> None:
        response = _client().get("/api/v1/jobs/%E2%80%AEover%00ride")
        assert response.status_code in (404, 422)
        body = response.json()
        assert body["code"] and body["title"]


class TestBodyAbuse:
    def test_malformed_json_is_a_422_problem(self) -> None:
        response = _client().post(
            "/api/v1/jobs",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422

    def test_a_json_body_that_is_not_an_object_is_a_422(self) -> None:
        response = _client().post("/api/v1/jobs", json=[1, 2, 3])
        assert response.status_code == 422

    def test_an_empty_body_is_a_422_not_a_500(self) -> None:
        response = _client().post("/api/v1/jobs")
        assert response.status_code == 422

    def test_wrong_content_type_is_rejected_cleanly(self) -> None:
        response = _client().post(
            "/api/v1/jobs",
            content=b"type=ingest",
            headers={"content-type": "text/plain"},
        )
        assert response.status_code == 422

    def test_a_huge_episode_body_is_rejected_for_shape_not_exhaustion(self) -> None:
        episode = {
            "task": "t",
            "robot": "r",
            "timestamps": [0.0] * 1_000_000,
            "observations": [[0.0]],
            "actions": [[0.1]],
        }
        response = _client().post(
            "/api/v1/jobs", json={"type": "ingest", "payload": {"episode": episode}}
        )
        assert response.status_code == 422

    def test_unknown_top_level_fields_are_rejected(self) -> None:
        response = _client().post(
            "/api/v1/jobs", json={"type": "ingest", "payload": {}, "admin": True}
        )
        assert response.status_code == 422
