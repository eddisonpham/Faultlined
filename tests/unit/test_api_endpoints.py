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


class ListingCatalogStub(CatalogStub):
    """Adds the list/count surface the Status, Jobs, and Artifacts pages read."""

    def __init__(self) -> None:
        self.states = ["succeeded", "failed", "succeeded", "queued"]

    def list_jobs(
        self,
        *,
        state: Any = None,
        job_type: str | None = None,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "id": f"job-{i}",
                "type": "ingest",
                "state": s,
                "correlation_id": f"corr-{i}",
                "error": None,
                "created_at": f"2026-09-28T00:00:0{i}Z",
                "started_at": None,
                "finished_at": None,
            }
            for i, s in enumerate(self.states)
        ]
        if state is not None:
            rows = [r for r in rows if r["state"] == state.value]
        return rows[:limit]

    def list_artifacts(self, *, limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        return [
            {
                "hash": "b" * 64,
                "size_bytes": 2048,
                "created_at": "2026-09-28T00:00:00Z",
                "episode_ids": ["episode-1"],
            }
        ][:limit]

    def list_episodes(
        self, *, limit: int = 50, state: str | None = None, flag: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            {
                "id": "episode-1",
                "episode_key": "episode_index=0",
                "format": "lerobot-v3",
                "state": "valid",
                "created_at": "2026-09-28T00:00:00Z",
                "frame_count": 303,
                "movement_score": 0.04,
                "jerk_score": 0.01,
                "stall_ratio": 0.1,
                "verdict": "smooth",
            }
        ]
        if flag == "jerky":
            return []
        return rows[:limit]

    def get_episode_quality(self, episode_id: str) -> dict[str, Any] | None:
        return {
            "episode_id": episode_id,
            "frame_count": 303,
            "movement_score": 0.04,
            "jerk_score": 0.01,
            "stall_ratio": 0.1,
            "verdict": "smooth",
            "dims": [
                {
                    "name": "action[0]",
                    "active": True,
                    "discrete": False,
                    "gripper": False,
                    "norm_delta_std": 0.01,
                    "mean_abs_delta_norm": 0.005,
                }
            ],
            "length_zscore": 0.2,
            "computed_at": "2026-09-29T00:00:00Z",
        }

    def quality_summary(self) -> dict[str, Any]:
        return {
            "episode_count": 1,
            "verdicts": {"smooth": 1},
            "length": {
                "count": 1,
                "mean": 303.0,
                "std": 0.0,
                "min": 303,
                "max": 303,
                "histogram": [{"lo": 303, "hi": 304, "count": 1}],
            },
            "speed_distribution": [
                {"episode_id": "episode-1", "movement_score": 0.04, "verdict": "smooth"}
            ],
            "heat_matrix": {
                "dims": ["action[0]"],
                "episodes": [{"episode_id": "episode-1", "values": [0.01]}],
            },
            "outliers": {
                "jerk": [
                    {
                        "episode_id": "episode-1",
                        "episode_key": "episode_index=0",
                        "value": 0.01,
                        "verdict": "smooth",
                    }
                ],
                "stall": [],
                "length": [],
            },
        }

    def count_jobs(self, state: Any) -> int:
        return sum(1 for s in self.states if s == state.value)

    def count_artifacts(self) -> int:
        return 1

    def count_episodes(self) -> int:
        return 1

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        self.canceled = job_id
        return {"id": job_id, "state": "canceled"}


@pytest.mark.contract
def test_ui_cancel_posts_and_redirects_back_to_the_job() -> None:
    """A 303 keeps a browser refresh from re-submitting the cancel."""
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = ListingCatalogStub()
    app.state.catalog = catalog
    client = TestClient(app)

    response = client.post("/ui/jobs/job-1/cancel", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs/job-1"
    assert catalog.canceled == "job-1"


@pytest.mark.contract
def test_ui_cancel_preserves_a_valid_theme_and_drops_a_hostile_one() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = ListingCatalogStub()
    client = TestClient(app)

    kept = client.post("/ui/jobs/job-1/cancel?theme=amber", follow_redirects=False)
    assert kept.headers["location"] == "/ui/jobs/job-1?theme=amber"

    dropped = client.post("/ui/jobs/job-1/cancel?theme=../../etc/passwd", follow_redirects=False)
    assert dropped.headers["location"] == "/ui/jobs/job-1"


@pytest.mark.contract
def test_ui_cancel_swallows_a_race_instead_of_showing_an_error_page() -> None:
    """The job may finish between render and click; the detail page shows the truth."""
    app = create_app(Settings(_env_file=None), initialize_database=False)

    class AlreadyGone(ListingCatalogStub):
        def request_cancel(self, job_id: str) -> dict[str, Any]:
            raise KeyError(job_id)

    app.state.catalog = AlreadyGone()
    response = TestClient(app).post("/ui/jobs/gone/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs/gone"


def _ui_client() -> TestClient:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = ListingCatalogStub()
    return TestClient(app)


@pytest.mark.contract
def test_jobs_list_endpoint_returns_items_and_cursor() -> None:
    body = _ui_client().get("/api/v1/jobs?limit=2").json()
    assert [i["id"] for i in body["items"]] == ["job-0", "job-1"]
    assert body["next_before"] == "2026-09-28T00:00:01Z"


@pytest.mark.contract
def test_jobs_list_filters_by_state() -> None:
    body = _ui_client().get("/api/v1/jobs?state=succeeded").json()
    assert {i["state"] for i in body["items"]} == {"succeeded"}


@pytest.mark.contract
def test_jobs_list_rejects_unknown_state() -> None:
    assert _ui_client().get("/api/v1/jobs?state=nonsense").status_code == 422


@pytest.mark.contract
def test_artifacts_list_endpoint_reports_referencing_episodes() -> None:
    body = _ui_client().get("/api/v1/artifacts").json()
    assert body["items"][0]["episode_ids"] == ["episode-1"]


@pytest.mark.contract
def test_status_endpoint_reports_queue_depth_and_resources() -> None:
    body = _ui_client().get("/api/v1/status").json()
    assert body["status"] == "ok"
    assert body["queue_depth"]["succeeded"] == 2
    assert body["artifact_count"] == 1
    assert "gpu_present" in body["resources"]


@pytest.mark.contract
def test_ui_pages_render() -> None:
    client = _ui_client()
    for path, expected in (
        ("/ui", "Queue depth by state"),
        ("/ui/jobs", "Trace"),
        ("/ui/artifacts", "content addressed"),
        ("/ui/faultlined.css", ".de-readouts"),
        ("/ui/vendor/terminal-ui/core.css", "--fine-use"),
        ("/ui/vendor/terminal-ui/theme-vt220.css", "--fine-use-bg"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.text, path


@pytest.mark.contract
def test_ui_jobs_state_filter_is_reflected_in_the_form() -> None:
    body = _ui_client().get("/ui/jobs?state=failed").text
    assert '<option value="failed" selected>' in body


@pytest.mark.contract
def test_ui_job_detail_and_missing_job() -> None:
    client = _ui_client()
    assert "corr-1" in client.get("/ui/jobs/job-1").text
    assert client.get("/ui/jobs/nope").status_code == 404


@pytest.mark.contract
def test_theme_selection_falls_back_for_unknown_names() -> None:
    client = _ui_client()
    assert 'data-theme="amber"' in client.get("/ui?theme=amber").text
    assert 'data-theme="vt220"' in client.get("/ui?theme=../../etc/passwd").text


@pytest.mark.contract
def test_ui_run_intelligence_pages_render() -> None:
    client = _ui_client()
    for path, expected in (
        ("/ui/metrics", "worker heartbeat"),
        ("/ui/episodes", "curation view"),
        ("/ui/episodes/episode-1", "Motion quality"),
        ("/ui/insights", "Cross-episode variance"),
        ("/ui/vendor/departure-mono/DepartureMono-Regular.woff2", None),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        if expected is not None:
            assert expected in response.text, path


@pytest.mark.contract
def test_ui_font_is_served_as_a_woff2() -> None:
    response = _ui_client().get("/ui/vendor/departure-mono/DepartureMono-Regular.woff2")
    assert response.headers["content-type"] == "font/woff2"
    assert response.content[:4] == b"wOF2"


@pytest.mark.contract
def test_ui_fragments_return_bodies_without_the_shell() -> None:
    client = _ui_client()
    for path, expected in (
        ("/ui/metrics", "de-readouts"),
        ("/ui/episodes", "de-table"),
        ("/ui/insights", "de-chart"),
    ):
        body = client.get(path, headers={"X-Fragment": "1"}).text
        assert "<html" not in body, path
        assert expected in body, path


@pytest.mark.contract
def test_ui_episode_filters_reject_unknown_values() -> None:
    client = _ui_client()
    assert client.get("/ui/episodes?state=nonsense").status_code == 422
    assert client.get("/ui/episodes?flag=nonsense").status_code == 422


@pytest.mark.contract
def test_ui_episode_detail_shows_quality_and_404s() -> None:
    client = _ui_client()
    body = client.get("/ui/episodes/episode-1").text
    assert "smooth" in body
    assert "action[0]" in body
    assert client.get("/ui/episodes/nope").status_code == 404


@pytest.mark.contract
def test_vendor_stylesheet_route_rejects_traversal() -> None:
    client = _ui_client()
    assert client.get("/ui/vendor/terminal-ui/nope.css").status_code == 404


@pytest.mark.contract
def test_fragment_requests_return_only_the_polling_body() -> None:
    client = _ui_client()
    fragment = client.get("/ui", headers={"X-Fragment": "1"})
    assert "<html" not in fragment.text
    assert "Queue depth by state" in fragment.text
