import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog.repository import SliceNameConflict
from data_engine.config import Settings
from data_engine.jobs.state import JobState

#: A build identity in the shape `builds/service.py::build_hash` produces, so a test
#: cannot assert against a hash the API would refuse (that is how the 64-character cap
#: survived: every fixture here was a short fake).
BUILD_HASH = "bld_" + "ab" * 32


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
    """`/` must not be a blank 404, and it must point at the operator UI (ADR 0021)."""
    body = _client().get("/").json()
    assert body["service"] == "faultlined"
    assert body["ui"] == "/ui"
    assert "docs" not in body, "the Swagger page is gone; the contract is a file, not a page"
    assert body["endpoints"]["submit_job"] == "POST /api/v1/jobs"


@pytest.mark.contract
def test_swagger_ui_is_not_served() -> None:
    """The browser surface is the operator UI. `/docs` must not reappear by default."""
    client = _client()
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    # The machine contract stays, because the drift check needs it.
    assert client.get("/openapi.json").status_code == 200


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
                "source_hash": "a" * 64,
                "artifact_hash": "b" * 64,
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

    def get_validation_results(self, episode_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "profile_hash": "a" * 64,
                "profile_name": "staged-strict",
                "profile_version": "1",
                "passed": False,
                "reason_codes": ["frame_count"],
                "violations": [{"code": "frame_count", "message": "expected at least 100 frames"}],
                "created_at": "2026-09-29T00:00:00Z",
            }
        ]

    def episodes_produced_by(self, job_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "episode-1",
                "episode_key": "episode_index=0",
                "source_hash": "a" * 64,
                "artifact_hash": "b" * 64,
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

    def job_report(self, job_id: str) -> dict[str, Any] | None:
        if job_id != "job-1":
            return None
        return {
            "job": {
                "id": "job-1",
                "type": "ingest",
                "state": "succeeded",
                "payload": {},
                "result": None,
                "error": None,
                "correlation_id": "corr-1",
                "created_at": "2026-09-28T00:00:00Z",
                "started_at": None,
                "finished_at": None,
            },
            "episodes": {
                "total": 1,
                "by_state": {"valid": 1},
                "verdicts": {"smooth": 1},
                "flags": {"jerky": 0, "stalled": 0},
                "length": {
                    "count": 1,
                    "mean": 303.0,
                    "std": 0.0,
                    "min": 303,
                    "max": 303,
                    "histogram": [],
                },
                "mean_movement_score": 0.04,
                "mean_jerk_score": 0.01,
                "mean_stall_ratio": 0.1,
            },
            "validation": {
                "episodes_evaluated": 1,
                "results": 1,
                "passed": 0,
                "failed": 1,
                "reason_codes": {"frame_count": 1},
            },
        }

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

    def count_jobs_by_state(self) -> dict[str, int]:
        return {state.value: self.count_jobs(state) for state in JobState}

    def count_artifacts(self) -> int:
        return 1

    def count_episodes(self) -> int:
        return 1

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        self.canceled = job_id
        return {"id": job_id, "state": "canceled"}


class IngestCatalogStub(ListingCatalogStub):
    """Records what a form queued, and reports a system with nothing in it."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[tuple[str, dict[str, Any]]] = []
        self.slices: list[dict[str, Any]] = []
        self.slice_conflict = False
        self.members: list[dict[str, Any]] = []
        self.fingerprint_rows: list[dict[str, Any]] = []
        self.build = {
            "hash": BUILD_HASH,
            "name": "pick-set",
            "episode_count": 0,
            "profile_hash": None,
            "code_commit": "abc1234",
            "job_id": "job-1",
            "created_at": "2026-10-06T00:00:00Z",
            "manifest": {},
        }

    def list_builds(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [self.build]

    def get_build(self, build_hash: str) -> dict[str, Any] | None:
        return self.build if build_hash == BUILD_HASH else None

    def build_episodes(self, build_hash: str) -> list[dict[str, Any]]:
        return self.members

    def episode_fingerprint_inputs(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(episode_ids)
        return [row for row in self.fingerprint_rows if str(row["id"]) in wanted]

    def list_slices(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def register_slice(
        self,
        *,
        name: str,
        notes: str = "",
        filter_config: dict[str, Any] | None = None,
        slice_id: str | None = None,
    ) -> dict[str, Any]:
        if self.slice_conflict:
            raise SliceNameConflict(f"slice name {name!r} already exists")
        self.slices.append({"name": name, "notes": notes, "filter_config": filter_config or {}})
        return {"id": "slice-1", "name": name, "notes": notes}

    def count_jobs(self, state: Any) -> int:
        return 0

    def count_jobs_by_state(self) -> dict[str, int]:
        return {state.value: 0 for state in JobState}

    def count_artifacts(self) -> int:
        return 0

    def count_episodes(self) -> int:
        return 0

    def submit_job(
        self,
        job_type: str,
        payload: dict[str, Any],
        key: Any,
        correlation_id: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], bool]:
        self.submitted.append((job_type, payload))
        return {"id": "job-new", "type": job_type, "state": "queued"}, True


def _ingest_client() -> tuple[TestClient, IngestCatalogStub]:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    catalog = IngestCatalogStub()
    app.state.catalog = catalog
    return TestClient(app), catalog


@pytest.mark.contract
def test_the_ingest_form_queues_a_job_from_the_browser() -> None:
    """The form is the only way in, so it has to actually work without a client."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs",
        data={"kind": "episode", "task": "pick_place", "robot": "arm", "frames": "3"},
        follow_redirects=False,
    )

    # 303, not 302: a refresh must not queue the same job a second time.
    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/jobs/job-new")
    assert len(catalog.submitted) == 1
    kind, payload = catalog.submitted[0]
    assert kind == "ingest"
    episode = payload["episode"]
    # The schema's own invariant, so the form cannot build an episode the JSON
    # endpoint would reject.
    assert len(episode["timestamps"]) == len(episode["observations"]) == 3
    assert len(episode["actions"]) == 3


@pytest.mark.contract
def test_the_ingest_form_accepts_a_dataset_path() -> None:
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs",
        data={"kind": "path", "source": "data/lerobot/demo"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    kind, payload = catalog.submitted[-1]
    assert kind == "ingest_source"
    assert payload["source"] == "data/lerobot/demo"


@pytest.mark.contract
def test_a_bad_ingest_submission_keeps_what_was_typed() -> None:
    """Losing the input on a validation error is what makes a form feel broken."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs",
        data={"kind": "episode", "task": "pick", "robot": "", "frames": "3"},
        follow_redirects=False,
    )

    assert response.status_code == 200, "a bad submission must re-render, not redirect"
    assert "task and a robot" in response.text
    assert 'value="pick"' in response.text, "the typed value must survive"
    assert catalog.submitted == [], "an invalid submission must not queue anything"


@pytest.mark.contract
def test_the_ingest_form_rejects_a_nonsense_frame_count() -> None:
    client, catalog = _ingest_client()

    for bad in ("abc", "0", "-4", "100000"):
        response = client.post(
            "/ui/jobs",
            data={"kind": "episode", "task": "t", "robot": "r", "frames": bad},
            follow_redirects=False,
        )
        assert response.status_code == 200, bad
        assert "queue ingest job" in response.text, bad
    assert catalog.submitted == []


@pytest.mark.contract
def test_the_ingest_form_does_not_pull_in_a_dependency() -> None:
    """Form(...) would require python-multipart for one urlencoded body.

    ADR 0014 is a no-build, no-extra-dependency frontend; a new runtime package
    to save three lines of urllib.parse is not a trade worth making.
    """
    source = Path("src/data_engine/api/app.py").read_text(encoding="utf-8")
    assert "from fastapi import" in source and "Form" not in source.split("\n")[12]
    assert "parse_qs" in source
    assert "multipart" not in Path("pyproject.toml").read_text(encoding="utf-8").lower()


@pytest.mark.contract
def test_the_validate_form_queues_a_job_from_the_browser() -> None:
    """Release blocker 1, first step: validation used to be curl-only (EXP-0017)."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs",
        data={
            "kind": "validate",
            "profile_name": "strict",
            "min_frames": "5",
            "episode_ids": "ep-1, ep-2",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    kind, payload = catalog.submitted[-1]
    assert kind == "validate"
    # The document the *API's own model* accepts, built from the form's fields: the
    # limits are typed, the selection is a list, and `enabled_rules` is absent so every
    # registered rule runs rather than whichever names a person happened to spell right.
    assert payload == {
        "episode_ids": ["ep-1", "ep-2"],
        "profile": {"name": "strict", "version": "1", "min_frames": 5},
    }


@pytest.mark.contract
def test_an_empty_validate_selection_means_everything() -> None:
    """The form inherits the API's meaning for an empty list, not a third convention."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs", data={"kind": "validate", "profile_name": "a"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert catalog.submitted[-1][1]["episode_ids"] == []


@pytest.mark.contract
def test_the_validate_form_reports_a_typed_limit_in_place() -> None:
    for bad in ("abc", "0", "-4"):
        client, catalog = _ingest_client()
        response = client.post(
            "/ui/jobs",
            data={
                "kind": "validate",
                "profile_name": "strict",
                "min_frames": bad,
                "episode_ids": "ep-1",
            },
            follow_redirects=False,
        )

        assert response.status_code == 200, bad
        assert "minimum frames" in response.text, bad
        assert 'name="min_frames"' in response.text and f'value="{bad}"' in response.text, bad
        assert catalog.submitted == [], bad


@pytest.mark.contract
def test_the_build_form_queues_a_job_from_the_browser() -> None:
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs",
        data={"kind": "build", "build_name": "pick-set", "episode_ids": "ep-1"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert catalog.submitted[-1] == (
        "build",
        {"name": "pick-set", "episode_ids": ["ep-1"], "profile": {}},
    )


@pytest.mark.contract
def test_a_build_without_a_name_is_reported_in_place() -> None:
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs", data={"kind": "build", "episode_ids": "ep-1"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert "build_name" in response.text, "the error must name the field"
    assert 'name="episode_ids"' in response.text and 'value="ep-1"' in response.text
    assert catalog.submitted == []


@pytest.mark.contract
def test_the_export_form_queues_the_builds_own_hash() -> None:
    """The hash the page posts is the canonical one, and the form cannot mistype it."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs", data={"kind": "export", "build_hash": BUILD_HASH}, follow_redirects=False
    )

    assert response.status_code == 303, response.text
    assert catalog.submitted[-1] == ("export", {"build_hash": BUILD_HASH})


@pytest.mark.contract
def test_the_export_form_refuses_a_hash_that_is_not_a_build_identity() -> None:
    """A bare digest is refused in the browser too, with a sentence, not a regex."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/jobs", data={"kind": "export", "build_hash": "ab" * 32}, follow_redirects=False
    )

    assert response.status_code == 200
    assert "bld_" in response.text and "64 hex" in response.text
    assert catalog.submitted == []


@pytest.mark.contract
def test_the_slice_form_saves_a_slice_from_the_browser() -> None:
    """`POST /api/v1/slices` was JSON-only; this is the same write, from the page."""
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/slices",
        data={"name": "good-picks", "notes": "for the next run", "state": "valid", "flag": "jerky"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    # The theme travels in the body, so the page the operator lands on is the one they
    # were using rather than the default.
    assert response.headers["location"].startswith("/ui/slices/slice-1")
    assert "theme=" in response.headers["location"]
    assert catalog.slices == [
        {
            "name": "good-picks",
            "notes": "for the next run",
            "filter_config": {"state": "valid", "flag": "jerky"},
        }
    ]


@pytest.mark.contract
def test_a_slice_without_a_name_is_reported_in_place() -> None:
    client, catalog = _ingest_client()

    response = client.post(
        "/ui/slices", data={"name": "   ", "state": "valid"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert "name" in response.text and "save slice" in response.text
    assert catalog.slices == []


@pytest.mark.contract
def test_a_duplicate_slice_name_is_reported_in_place() -> None:
    """The JSON API answers 409 here; a browser gets the form back with the name in it."""
    client, catalog = _ingest_client()
    catalog.slice_conflict = True

    response = client.post("/ui/slices", data={"name": "taken"}, follow_redirects=False)

    assert response.status_code == 200
    assert "already exists" in response.text
    assert 'value="taken"' in response.text
    assert catalog.slices == []


@pytest.mark.contract
def test_the_whole_loop_is_reachable_without_a_json_call() -> None:
    """The audit's acceptance criterion, as a test: every stage has a form."""
    client, _catalog = _ingest_client()

    episodes = client.get("/ui/episodes").text
    builds = client.get("/ui/builds").text
    detail = client.get(f"/ui/builds/{BUILD_HASH}").text
    slices = client.get("/ui/slices").text

    assert 'action="/ui/jobs"' in episodes and 'value="validate"' in episodes
    assert 'action="/ui/jobs"' in builds and 'value="build"' in builds
    assert 'value="export"' in detail and BUILD_HASH in detail
    assert 'action="/ui/slices"' in slices and 'name="name"' in slices


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
def test_episodes_catalog_endpoint_exposes_quality_columns() -> None:
    body = _ui_client().get("/api/v1/episodes").json()
    item = body["items"][0]
    assert item["id"] == "episode-1"
    assert item["verdict"] == "smooth"
    assert item["frame_count"] == 303


@pytest.mark.contract
def test_episodes_catalog_rejects_bad_filters_and_limits() -> None:
    client = _ui_client()
    assert client.get("/api/v1/episodes?state=nonsense").status_code == 422
    assert client.get("/api/v1/episodes?flag=nonsense").status_code == 422
    assert client.get("/api/v1/episodes?limit=0").status_code == 422
    assert client.get("/api/v1/episodes?limit=501").status_code == 422


@pytest.mark.contract
def test_episode_validation_endpoint_exposes_quarantine_reasons() -> None:
    body = _ui_client().get("/api/v1/episodes/episode-1/validation").json()
    assert body["episode_id"] == "episode-1"
    result = body["results"][0]
    assert result["passed"] is False
    assert result["reason_codes"] == ["frame_count"]
    assert result["violations"][0]["code"] == "frame_count"


@pytest.mark.contract
def test_episode_validation_unknown_episode_is_404() -> None:
    assert _ui_client().get("/api/v1/episodes/missing/validation").status_code == 404


@pytest.mark.contract
def test_job_episodes_endpoint_follows_lineage() -> None:
    body = _ui_client().get("/api/v1/jobs/job-1/episodes").json()
    assert body["items"][0]["id"] == "episode-1"


@pytest.mark.contract
def test_job_episodes_unknown_job_is_404() -> None:
    assert _ui_client().get("/api/v1/jobs/missing/episodes").status_code == 404


@pytest.mark.contract
def test_job_report_summarizes_a_run() -> None:
    body = _ui_client().get("/api/v1/jobs/job-1/report").json()
    assert body["job"]["id"] == "job-1"
    assert body["episodes"]["total"] == 1
    assert body["episodes"]["verdicts"] == {"smooth": 1}
    assert body["episodes"]["mean_jerk_score"] == 0.01
    assert body["validation"]["failed"] == 1
    assert body["validation"]["reason_codes"] == {"frame_count": 1}


@pytest.mark.contract
def test_job_report_unknown_job_is_404() -> None:
    assert _ui_client().get("/api/v1/jobs/missing/report").status_code == 404


@pytest.mark.contract
def test_episode_export_returns_a_curated_manifest() -> None:
    body = _ui_client().get("/api/v1/episodes/export").json()
    assert body["count"] == 1
    assert body["filters"] == {"state": None, "flag": None, "limit": 100}
    item = body["items"][0]
    assert item["id"] == "episode-1"
    assert item["source_hash"] == "a" * 64
    assert item["artifact_hash"] == "b" * 64
    assert item["verdict"] == "smooth"


@pytest.mark.contract
def test_episode_export_filters_and_rejects_bad_input() -> None:
    client = _ui_client()
    empty = client.get("/api/v1/episodes/export?flag=jerky").json()
    assert empty["count"] == 0
    assert empty["items"] == []
    assert client.get("/api/v1/episodes/export?state=nonsense").status_code == 422
    assert client.get("/api/v1/episodes/export?limit=0").status_code == 422
    assert client.get("/api/v1/episodes/export?limit=501").status_code == 422


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
def test_ui_job_detail_lists_produced_episodes() -> None:
    text = _ui_client().get("/ui/jobs/job-1").text
    assert "Produced episodes" in text
    assert "/ui/episodes/episode-1" in text


@pytest.mark.contract
def test_ui_job_detail_shows_the_run_report() -> None:
    text = _ui_client().get("/ui/jobs/job-1").text
    assert "Run report" in text
    assert "0 passed // 1 failed" in text
    assert "frame_count &times; 1" in text


@pytest.mark.contract
def test_ui_episode_detail_shows_validation_section() -> None:
    text = _ui_client().get("/ui/episodes/episode-1").text
    assert "Validation" in text
    assert "QUARANTINED" in text
    assert "frame_count" in text


@pytest.mark.contract
def test_ui_episode_detail_without_validation_says_so() -> None:
    class NoValidation(ListingCatalogStub):
        def get_validation_results(self, episode_id: str) -> list[dict[str, Any]]:
            return []

    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = NoValidation()
    text = TestClient(app).get("/ui/episodes/episode-1").text
    assert "no validation runs" in text


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


# ----------------------------------------------- build redundancy (ADR 0032)


def _fingerprint_row(episode_id: str, **overrides: Any) -> dict[str, Any]:
    """One `episode_fingerprint_inputs` row in the shape the catalog returns."""
    trace = [[[index * 0.1, math.sin(index / 4.0)] for index in range(40)]]
    return {
        "id": episode_id,
        "task": f"pick {episode_id}",
        "verdict": "smooth",
        "judged_dims": 6,
        "stall_ratio": 0.0,
        "gap_ratio": 0.0,
        "dims": [
            {
                "name": f"/joint_states.position[{index}]",
                "active": True,
                "discrete": False,
                "gripper": False,
                "norm_delta_std": 0.03,
                "mean_abs_delta_norm": 0.02,
            }
            for index in range(2)
        ],
        "motion_trace": trace,
        **overrides,
    }


def _build_with_members(*episode_ids: str) -> tuple[TestClient, IngestCatalogStub]:
    client, catalog = _ingest_client()
    catalog.members = [
        {"episode_id": episode_id, "ordinal": index} for index, episode_id in enumerate(episode_ids)
    ]
    catalog.fingerprint_rows = [_fingerprint_row(episode_id) for episode_id in episode_ids]
    return client, catalog


@pytest.mark.contract
def test_the_redundancy_route_reports_near_duplicates_for_a_build() -> None:
    """The API's own report is what the build page renders, so it is asserted here."""
    client, _ = _build_with_members("ep-a", "ep-b")

    body = client.get(f"/api/v1/builds/{BUILD_HASH}/redundancy").json()

    assert body["build_hash"] == BUILD_HASH
    assert body["episode_count"] == 2
    assert body["distinct_count"] == 1
    assert body["redundant_count"] == 1
    assert body["groups"][0]["duplicates"][0]["episode_id"] == "ep-b"


@pytest.mark.contract
def test_the_redundancy_route_reports_distinct_episodes_as_distinct() -> None:
    client, catalog = _build_with_members("ep-a", "ep-b")
    # A different behaviour: a spike where the other has a sweep.
    catalog.fingerprint_rows[1]["motion_trace"] = [
        [[index * 0.1, 0.0 if index < 30 else 1.0] for index in range(40)]
    ]

    body = client.get(f"/api/v1/builds/{BUILD_HASH}/redundancy").json()

    assert body["groups"] == []
    assert body["distinct_count"] == 2
    assert body["reduction_ratio"] == 0.0


@pytest.mark.contract
def test_the_redundancy_route_is_404_for_an_unknown_build() -> None:
    client, _ = _build_with_members("ep-a")
    assert client.get(f"/api/v1/builds/{'c' * 68}/redundancy").status_code == 404


@pytest.mark.contract
def test_the_redundancy_route_refuses_a_threshold_it_cannot_honour() -> None:
    """A distance above 1.0 is not producible, so a threshold above it is a client bug, not a
    request to be clamped silently."""
    client, _ = _build_with_members("ep-a", "ep-b")
    assert client.get(f"/api/v1/builds/{BUILD_HASH}/redundancy?threshold=2.0").status_code == 422
    assert client.get(f"/api/v1/builds/{BUILD_HASH}/redundancy?threshold=-0.5").status_code == 422


@pytest.mark.contract
def test_the_redundancy_route_honours_an_explicit_threshold() -> None:
    client, _ = _build_with_members("ep-a", "ep-b")
    body = client.get(f"/api/v1/builds/{BUILD_HASH}/redundancy?threshold=0.0").json()
    assert body["threshold"] == 0.0
    assert body["redundant_count"] == 1


@pytest.mark.contract
def test_the_build_page_shows_the_redundancy_report_it_serves() -> None:
    client, _ = _build_with_members("ep-a", "ep-b")

    body = client.get(f"/ui/builds/{BUILD_HASH}").text

    assert "Redundancy" in body
    assert "1 of 2 distinct" in body
    assert 'href="/ui/episodes/ep-b"' in body
    assert "read-only" in body


@pytest.mark.contract
def test_a_build_with_nothing_to_compare_says_so_rather_than_showing_zeros() -> None:
    client, _ = _build_with_members("ep-a")
    body = client.get(f"/ui/builds/{BUILD_HASH}").text
    assert "fewer than two scored episodes" in body
