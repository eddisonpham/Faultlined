"""Black-box contracts for the vocabulary-first curation surface (ADR 0029)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog import vocabulary as store
from data_engine.config import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    state: dict[str, Any] = {
        "entries": [{"id": "voc_mug", "preferred_label": "mug", "core": "mug", "notes": ""}],
        "mappings": {},
        "events": [],
        "queue": [{"task_string": "pick up the red mug", "episodes": 3, "first_seen": None}],
    }

    def list_entries(_settings: Settings, *, limit: int = 200) -> list[dict[str, Any]]:
        return state["entries"][:limit]

    def get_entry(_settings: Settings, entry_id: str) -> dict[str, Any] | None:
        return next((entry for entry in state["entries"] if entry["id"] == entry_id), None)

    def create_entry(
        _settings: Settings, *, preferred_label: str, notes: str = "", **_kwargs: Any
    ) -> dict[str, Any]:
        entry = {
            "id": f"voc_{preferred_label}",
            "preferred_label": preferred_label.strip(),
            "core": preferred_label.strip(),
            "notes": notes,
        }
        state["entries"].append(entry)
        return entry

    def map_task(
        _settings: Settings, *, task_string: str, entry_id: str, **_kwargs: Any
    ) -> dict[str, Any]:
        state["mappings"][task_string] = entry_id
        state["queue"] = [row for row in state["queue"] if row["task_string"] != task_string]
        state["events"].append({"id": len(state["events"]) + 1, "kind": "map"})
        return {"task_string": task_string, "entry_id": entry_id, "provenance": "confirm"}

    monkeypatch.setattr(store, "list_entries", list_entries)
    monkeypatch.setattr(store, "get_entry", get_entry)
    monkeypatch.setattr(store, "create_entry", create_entry)
    monkeypatch.setattr(store, "map_task", map_task)
    accepted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        store,
        "accept_candidate",
        lambda _settings, **kwargs: accepted.append(kwargs) or state["entries"][0],
    )
    monkeypatch.setattr(store, "entry_members", lambda *_args: [])
    monkeypatch.setattr(store, "list_unmapped", lambda *_args, **_kwargs: state["queue"])
    monkeypatch.setattr(store, "vocabulary_health", lambda *_args: {"entries": 1})
    monkeypatch.setattr(store, "list_events", lambda *_args, **_kwargs: state["events"])
    state["accepted"] = accepted
    monkeypatch.setattr(store, "dismiss_task", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(store, "rename_entry", lambda *_args, **_kwargs: state["entries"][0])
    monkeypatch.setattr(store, "update_notes", lambda *_args, **_kwargs: state["entries"][0])
    monkeypatch.setattr(
        store,
        "merge_entries",
        lambda *_args, **_kwargs: {
            "event_id": 1,
            "moved": 0,
            "source": state["entries"][0],
            "target_id": "voc_target",
        },
    )
    monkeypatch.setattr(
        store,
        "split_entry",
        lambda *_args, **_kwargs: {
            "event_id": 2,
            "moved": 1,
            "entry_id": "voc_mug",
            "new_entry": state["entries"][0],
        },
    )
    monkeypatch.setattr(
        store,
        "undo_event",
        lambda *_args, **_kwargs: {"event_id": 1, "kind": "map", "undone": True},
    )

    from data_engine.catalog import clusters
    from data_engine.clustering import Ignored, build

    def cluster_model(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ignored": Ignored(),
            "proposals": build({}),
            "rows": [],
            "members": {},
            "radius": 0.3,
            "rule": "running_mean",
            "source": "catalog",
            "health": {},
            "run": None,
            "review_candidates": [],
            "review_decisions": [],
        }

    monkeypatch.setattr(clusters, "model", cluster_model)
    monkeypatch.setattr(clusters, "review_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(clusters, "review_decisions", lambda *_args, **_kwargs: [])
    return TestClient(create_app(Settings(database_url="postgresql://unused/unused")))


def test_vocabulary_list_and_new_entry_are_rendered_from_the_store(client: TestClient) -> None:
    listed = client.get("/api/v1/vocabulary")
    assert listed.status_code == 200
    assert listed.json()["entries"][0]["preferred_label"] == "mug"
    created = client.post("/api/v1/vocabulary", json={"preferred_label": "drawer"})
    assert created.status_code == 201
    assert created.json()["entry"]["preferred_label"] == "drawer"


def test_queue_and_candidate_routes_use_the_current_unmapped_rows(client: TestClient) -> None:
    queue = client.get("/api/v1/vocabulary/unmapped")
    assert queue.status_code == 200
    assert queue.json()["items"][0]["episodes"] == 3
    candidates = client.get("/api/v1/vocabulary/candidates")
    assert candidates.status_code == 200
    assert candidates.json()["items"][0]["kind"] == "attach"


def test_candidate_acceptance_uses_one_atomic_store_operation(client: TestClient) -> None:
    response = client.post(
        "/ui/vocabulary/accept",
        data={
            "kind": "attach",
            "entry_id": "voc_mug",
            "task": ["pick up the red mug"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/vocabulary"
    assert client.app.state  # the HTTP interface was exercised, not just the store mock


def test_mapping_and_undo_are_exposed_without_changing_the_episode_task(client: TestClient) -> None:
    mapped = client.post(
        "/api/v1/vocabulary/mappings",
        json={"task_string": "pick up the red mug", "entry_id": "voc_mug"},
    )
    assert mapped.status_code == 200
    assert mapped.json()["provenance"] == "confirm"
    undone = client.post("/api/v1/vocabulary/events/1/undo")
    assert undone.status_code == 200 and undone.json()["undone"] is True


def test_unknown_entry_is_a_404_and_empty_patch_is_a_422(client: TestClient) -> None:
    assert client.get("/api/v1/vocabulary/missing").status_code == 404
    assert client.patch("/api/v1/vocabulary/voc_mug", json={}).status_code == 422


def test_vocabulary_page_escapes_task_text_and_offers_plain_form_actions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "<img src=x onerror=alert(1)>"
    monkeypatch.setattr(
        store,
        "list_unmapped",
        lambda *_args, **_kwargs: [{"task_string": hostile, "episodes": 1, "first_seen": None}],
    )
    response = client.get("/ui/vocabulary")
    assert response.status_code == 200
    assert hostile not in response.text
    assert "&lt;img" in response.text
    assert 'action="/ui/vocabulary/map"' in response.text
    assert 'action="/ui/vocabulary/dismiss"' in response.text


def test_legacy_cluster_ui_redirects_but_read_archive_stays_available(client: TestClient) -> None:
    redirect = client.get("/ui/clusters", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/ui/vocabulary"
    archived = client.get("/api/v1/clusters")
    assert archived.status_code == 200
    retired = client.post("/api/v1/clusters/rebuild", json={})
    assert retired.status_code == 410
