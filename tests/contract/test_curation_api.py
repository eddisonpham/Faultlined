"""Contract tests for curated slices and the validation-failures read view.

These are blackbox over the app: a fake catalog is wired into `create_app` and the
tests drive it through `TestClient`, so routing, serialization, and the error
handler are all exercised the way a client experiences them. The SQL behind these
shapes is covered by `tests/integration/test_curation.py` against a real database.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog.repository import SliceNameConflict
from data_engine.config import Settings

_SLICE = {
    "id": "slice-1",
    "name": "smooth-survivors",
    "notes": "episodes that passed motion-quality curation",
    "filter_config": {"state": "valid", "flag": ""},
    "created_at": "2026-09-29T00:00:00Z",
    "updated_at": "2026-09-29T00:00:00Z",
    "member_count": 1,
}

_EPISODE = {
    "id": "episode-1",
    "episode_key": "episode_index=0",
    "source_hash": "a" * 64,
    "artifact_hash": "b" * 64,
    "format": "lerobot-v3",
    "state": "quarantined",
    "created_at": "2026-09-29T00:00:00Z",
    "frame_count": 12,
    "movement_score": 0.001,
    "jerk_score": 0.09,
    "stall_ratio": 0.8,
    "verdict": "jerky",
}


class CurationCatalogStub:
    """Deterministic curation read/write surface for the new endpoints."""

    def __init__(self) -> None:
        self.slices: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []

    def failure_summary(self) -> dict[str, Any] | None:
        return {
            "reason_codes": {"TOO_FEW_FRAMES": 2, "VALIDATION_FAILED": 1},
            "by_profile": [{"profile_name": "staged-strict", "failed": 3}],
            "by_format": [{"format": "lerobot-v3", "failed": 3}],
            "quarantined_count": 3,
            "episodes_evaluated": 5,
        }

    def failing_episodes(
        self, *, limit: int = 50, before: Any = None, reason_code: str | None = None
    ) -> list[dict[str, Any]]:
        if reason_code is not None and "TOO_FEW_FRAMES" not in reason_code:
            return []
        return [{**_EPISODE, "profile_name": "staged-strict", "profile_version": "1"}][:limit]

    def list_slices(self, *, limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        return list(self.slices.values())[:limit]

    def get_slice(self, slice_id: str) -> dict[str, Any] | None:
        return self.slices.get(slice_id)

    def register_slice(
        self,
        *,
        name: str,
        notes: str = "",
        filter_config: dict[str, Any] | None = None,
        slice_id: str | None = None,
    ) -> dict[str, Any]:
        if any(row["name"] == name for row in self.slices.values()):
            raise SliceNameConflict(f"slice name {name!r} already exists")
        row = {
            "id": slice_id or f"slice-{len(self.slices) + 1}",
            "name": name,
            "notes": notes,
            "filter_config": dict(filter_config or {}),
            "created_at": "2026-09-29T00:00:00Z",
            "updated_at": "2026-09-29T00:00:00Z",
            "member_count": 0,
        }
        self.slices[row["id"]] = row
        self.created.append(row)
        return row

    def update_slice(
        self,
        slice_id: str,
        *,
        name: str | None = None,
        notes: str | None = None,
        filter_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = self.slices.get(slice_id)
        if row is None:
            return None
        for key, value in (("name", name), ("notes", notes), ("filter_config", filter_config)):
            if value is not None:
                row[key] = value
        return row

    def delete_slice(self, slice_id: str) -> bool:
        return self.slices.pop(slice_id, None) is not None

    def slice_manifest(
        self, slice_id: str, *, limit: int = 100, before: Any = None
    ) -> dict[str, Any] | None:
        row = self.slices.get(slice_id)
        if row is None:
            return None
        return {
            "slice_id": slice_id,
            "name": row["name"],
            "generated_at": "2026-09-29T00:00:00Z",
            "filters": {"state": "valid", "flag": ""},
            "count": 1,
            "items": [{**_EPISODE, "state": "valid"}],
        }


def _client(catalog: CurationCatalogStub | None = None) -> TestClient:
    app = create_app(
        Settings(_env_file=None),
        initialize_database=False,
        catalog=catalog or CurationCatalogStub(),  # type: ignore[arg-type]
    )
    return TestClient(app)


# ---------------------------------------------------------------- failures


@pytest.mark.contract
def test_failures_summary_reports_reason_codes_and_profiles() -> None:
    response = _client().get("/api/v1/failures")
    assert response.status_code == 200
    body = response.json()
    assert body["quarantined_count"] == 3
    assert body["episodes_evaluated"] == 5
    assert body["reason_codes"]["TOO_FEW_FRAMES"] == 2
    assert body["by_profile"][0]["profile_name"] == "staged-strict"


@pytest.mark.contract
def test_failing_episodes_carry_the_codes_that_quarantined_them() -> None:
    body = _client().get("/api/v1/failures/episodes?limit=10").json()
    item = body["items"][0]
    assert item["id"] == "episode-1"
    assert item["state"] == "quarantined"
    assert item["profile_name"] == "staged-strict"
    assert item["verdict"] == "jerky"


@pytest.mark.contract
def test_failing_episodes_filters_by_reason_code() -> None:
    client = _client()
    assert client.get("/api/v1/failures/episodes?reason_code=TOO_FEW_FRAMES").json()["items"]
    assert (
        client.get("/api/v1/failures/episodes?reason_code=VALIDATION_FAILED").json()["items"] == []
    )


@pytest.mark.contract
def test_failing_episodes_rejects_unknown_codes_and_limits() -> None:
    client = _client()
    assert client.get("/api/v1/failures/episodes?reason_code=NOPE").status_code == 422
    assert client.get("/api/v1/failures/episodes?limit=0").status_code == 422
    assert client.get("/api/v1/failures/episodes?limit=501").status_code == 422


# ---------------------------------------------------------------- slices


@pytest.mark.contract
def test_creating_a_slice_returns_its_canonical_shape() -> None:
    response = _client().post(
        "/api/v1/slices",
        json={"name": "smooth-survivors", "notes": "curation", "filter_config": {"state": "valid"}},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "smooth-survivors"
    assert body["filter_config"] == {"state": "valid"}
    assert body["member_count"] == 0
    assert body["created_at"] and body["updated_at"]


@pytest.mark.contract
def test_a_duplicate_slice_name_is_a_conflict() -> None:
    catalog = CurationCatalogStub()
    client = _client(catalog)
    payload = {"name": "dup", "filter_config": {}}
    assert client.post("/api/v1/slices", json=payload).status_code == 201
    second = client.post("/api/v1/slices", json=payload)
    assert second.status_code == 409
    assert second.json()["code"] == "SLICE_NAME_CONFLICT"


@pytest.mark.contract
def test_slice_requests_forbid_unknown_fields() -> None:
    client = _client()
    assert client.post("/api/v1/slices", json={"name": "x", "surprise": 1}).status_code == 422
    assert client.post("/api/v1/slices", json={"name": ""}).status_code == 422
    assert client.patch("/api/v1/slices/slice-1", json={"surprise": 1}).status_code == 422


@pytest.mark.contract
def test_slice_lifecycle_read_update_delete() -> None:
    catalog = CurationCatalogStub()
    client = _client(catalog)
    slice_id = client.post("/api/v1/slices", json={"name": "curate", "filter_config": {}}).json()[
        "id"
    ]

    listed = client.get("/api/v1/slices").json()
    assert [item["id"] for item in listed["items"]] == [slice_id]

    detail = client.get(f"/api/v1/slices/{slice_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "curate"

    patched = client.patch(f"/api/v1/slices/{slice_id}", json={"notes": "tuned"})
    assert patched.status_code == 200
    assert patched.json()["notes"] == "tuned"

    assert client.delete(f"/api/v1/slices/{slice_id}").status_code == 204
    assert client.get(f"/api/v1/slices/{slice_id}").status_code == 404


@pytest.mark.contract
def test_unknown_slices_are_404s_including_the_manifest() -> None:
    client = _client()
    assert client.get("/api/v1/slices/nope").status_code == 404
    assert client.get("/api/v1/slices/nope/manifest").status_code == 404
    assert client.patch("/api/v1/slices/nope", json={"notes": "x"}).status_code == 404
    assert client.delete("/api/v1/slices/nope").status_code == 404


@pytest.mark.contract
def test_slice_manifest_reuses_the_export_shape_with_identity() -> None:
    catalog = CurationCatalogStub()
    client = _client(catalog)
    slice_id = client.post(
        "/api/v1/slices", json={"name": "survivors", "filter_config": {"state": "valid"}}
    ).json()["id"]

    body = client.get(f"/api/v1/slices/{slice_id}/manifest").json()
    assert body["slice_id"] == slice_id
    assert body["name"] == "survivors"
    assert body["filters"] == {"state": "valid", "flag": ""}
    assert body["generated_at"]
    item = body["items"][0]
    assert item["source_hash"] == "a" * 64
    assert item["artifact_hash"] == "b" * 64


@pytest.mark.contract
def test_slice_listing_bounds_its_page() -> None:
    client = _client()
    assert client.get("/api/v1/slices?limit=0").status_code == 422
    assert client.get("/api/v1/slices?limit=501").status_code == 422
    assert client.get("/api/v1/slices?before=not-a-timestamp").status_code == 422


# ---------------------------------------------------------------- ui


@pytest.mark.contract
def test_curation_pages_render_and_poll_by_fragment() -> None:
    catalog = CurationCatalogStub()
    client = _client(catalog)
    client.post("/api/v1/slices", json={"name": "page-slice", "filter_config": {}})

    failures = client.get("/ui/failures")
    assert failures.status_code == 200
    assert "Reason codes" in failures.text
    assert "TOO_FEW_FRAMES" in failures.text
    assert "Quarantined episodes" in failures.text

    slices = client.get("/ui/slices")
    assert slices.status_code == 200
    assert "page-slice" in slices.text
    assert "/api/v1/slices/" in slices.text

    for path in ("/ui/failures", "/ui/slices"):
        fragment = client.get(path, headers={"X-Fragment": "1"})
        assert "<html" not in fragment.text, path
