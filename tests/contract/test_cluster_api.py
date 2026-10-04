"""The cluster API is a read-only archive after the vocabulary cutover (ADR 0029)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog import clusters
from data_engine.clustering import Ignored, build
from data_engine.config import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def model(_settings: Settings, **_kwargs: Any) -> dict[str, Any]:
        proposals = build({"pick up the red mug": 3})
        return {
            "ignored": Ignored(),
            "rows": [
                {
                    "key": item.key,
                    "core": item.core,
                    "label": item.label,
                    "episodes": item.size,
                    "task_count": item.task_count,
                    "merged_cores": list(item.merged_cores),
                }
                for item in proposals.proposals
            ],
            "members": {
                item.key: [
                    {
                        "task": member.task,
                        "core": member.core,
                        "episodes": member.episodes,
                        "verb": "",
                        "colours": [],
                    }
                    for member in item.members
                ]
                for item in proposals.proposals
            },
            "radius": 0.3,
            "rule": "running_mean",
            "source": "catalog",
            "health": {"orphaned": 0, "truncated": False},
            "run": None,
            "review_candidates": [],
            "review_decisions": [],
        }

    monkeypatch.setattr(clusters, "model", model)
    monkeypatch.setattr(
        clusters,
        "get_proposal",
        lambda _settings, key: next(
            (row for row in model(_settings)["rows"] if row["key"] == key), None
        ),
    )
    monkeypatch.setattr(
        clusters,
        "proposal_members",
        lambda _settings, key: model(_settings)["members"].get(key, []),
    )
    monkeypatch.setattr(clusters, "review_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(clusters, "review_decisions", lambda *_args, **_kwargs: [])
    return TestClient(create_app(Settings(database_url="postgresql://unused/unused")))


def test_cluster_archive_still_serves_list_and_detail_reads(client: TestClient) -> None:
    listed = client.get("/api/v1/clusters")
    assert listed.status_code == 200
    key = listed.json()["proposals"][0]["key"]
    detail = client.get(f"/api/v1/clusters/{key}")
    assert detail.status_code == 200
    assert detail.json()["members"][0]["task"] == "pick up the red mug"


def test_cluster_mutations_are_gone_after_vocabulary_cutover(client: TestClient) -> None:
    assert client.post("/api/v1/clusters/rebuild", json={}).status_code == 410
    assert (
        client.post(
            "/api/v1/clusters/review",
            json={"tasks": ["pick up the red mug"], "disposition": "dismissed"},
        ).status_code
        == 410
    )
    assert (
        client.post("/api/v1/clusters/deadbeef/confirm", json={"label": "mug"}).status_code == 410
    )


def test_legacy_cluster_ui_routes_redirect_to_vocabulary(client: TestClient) -> None:
    response = client.get("/ui/clusters", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/vocabulary"
    detail = client.get("/ui/clusters/old-key", follow_redirects=False)
    assert detail.status_code == 307
    assert detail.headers["location"] == "/ui/vocabulary?from_cluster=old-key"
    action = client.post("/ui/clusters/rebuild", follow_redirects=False)
    assert action.status_code == 303
    assert action.headers["location"] == "/ui/vocabulary"
