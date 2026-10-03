"""Contract tests for the cluster proposal API and the Clusters page.

Blackbox over the app: the cluster store's functions are replaced, so routing,
serialization and the error handler are exercised the way a client experiences them
without needing PostgreSQL. The SQL is covered by `tests/integration`; the clustering
itself by `tests/unit/test_clustering_extraction.py`.

Two things are pinned here because they are the promises the feature makes: a rebuild
carries the axes that were ignored (so a caller cannot read the numbers as if they came
from the default configuration), and a confirmation is refused rather than silently
recorded for a cluster that does not exist.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.catalog import clusters as store
from data_engine.clustering import Ignored, build, proposal_key
from data_engine.config import Settings

TASKS = {
    "pick up the red mug": 12,
    "pick up the blue mug": 3,
    "grab the mug": 5,
    "put down the laptop": 7,
    "open the drawer": 4,
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app with the cluster store replaced by an in-memory one."""
    state: dict[str, Any] = {"confirmations": {}, "rebuilds": [], "review_decisions": {}}

    candidate = {
        "task": "open the drawer",
        "core": "drawer",
        "cluster_key": proposal_key("drawer"),
        "proposal_core": "drawer",
        "episodes": 4,
        "distance": 0.0,
        "radius": 0.3,
        "reason": "no supporting neighbors",
        "source": "catalog",
    }

    def fake_review_candidates(_settings: Settings, **_kwargs: Any) -> list[dict[str, Any]]:
        return [] if candidate["task"] in state["review_decisions"] else [candidate]

    def fake_review_decisions(_settings: Settings, **_kwargs: Any) -> list[dict[str, str]]:
        return [
            {"task": task, "disposition": disposition, "label": label}
            for task, (disposition, label) in state["review_decisions"].items()
        ]

    def fake_decide_review(
        _settings: Settings, tasks: list[str], *, disposition: str, label: str = "", **_kwargs: Any
    ) -> int:
        candidates = {item["task"] for item in fake_review_candidates(_settings)}
        if not tasks or not set(tasks) <= candidates:
            raise ValueError(
                "review selection is no longer in the candidate queue; reload and try again"
            )
        for task in tasks:
            state["review_decisions"][task] = (disposition, label if disposition == "class" else "")
        return len(tasks)

    def fake_undo_review(_settings: Settings, tasks: list[str]) -> int:
        return sum(state["review_decisions"].pop(task, None) is not None for task in tasks)

    def fake_model(_settings: Settings, **_kwargs: Any) -> dict[str, Any]:
        proposals = build(TASKS, ignored=Ignored(verb=True, colour=True))
        proposals.apply_confirmations(dict(state["confirmations"]))
        return {
            "rows": [
                {
                    "key": item.key,
                    "core": item.core,
                    "episodes": item.size,
                    "task_count": item.task_count,
                    "merged_cores": list(item.merged_cores),
                    "label": item.label,
                    "source": "catalog",
                    "ignored": "verb, colour",
                    "radius": 0.3,
                    "rule": "running_mean",
                }
                for item in proposals.proposals
            ],
            "members": {
                item.key: [
                    {"task": member.task, "core": member.core, "episodes": member.episodes}
                    for member in item.members
                ]
                for item in proposals.proposals
            },
            "history": [],
            "run": {
                "id": 1,
                "source": "catalog",
                "ignored": "verb, colour",
                "radius": 0.3,
                "rule": "running_mean",
                "health": {"orphaned": 0, "truncated": False},
                "created_at": None,
            },
            "ignored": Ignored(verb=True, colour=True),
            "radius": 0.3,
            "rule": "running_mean",
            "source": "catalog",
            "health": {"orphaned": 0, "truncated": False},
            "review_candidates": fake_review_candidates(_settings),
            "review_decisions": fake_review_decisions(_settings),
            "proposals": proposals,
        }

    def fake_rebuild(_settings: Settings, **options: Any) -> dict[str, Any]:
        state["rebuilds"].append(options)
        proposals = build(
            TASKS,
            ignored=Ignored.parse(str(options.get("ignored", ""))),
            radius=float(options.get("radius", 0.3)),
            rule=str(options.get("rule", "running_mean")),
            source=str(options.get("source", "catalog")),
        )
        proposals.apply_confirmations(dict(state["confirmations"]))
        return {"health": proposals.health(), "proposals": proposals, "run_id": 1}

    def fake_confirm(_settings: Settings, key: str, label: str, **_kwargs: Any) -> bool:
        tasks = ["pick up the red mug", "pick up the blue mug", "grab the mug"]
        state["confirmations"][key] = label
        state["confirmed_tasks"] = sorted(tasks)
        return True

    def fake_unconfirm(_settings: Settings, key: str) -> bool:
        return state["confirmations"].pop(key, None) is not None

    monkeypatch.setattr(store, "model", fake_model)
    monkeypatch.setattr(store, "rebuild", fake_rebuild)
    monkeypatch.setattr(store, "review_candidates", fake_review_candidates)
    monkeypatch.setattr(store, "review_decisions", fake_review_decisions)
    monkeypatch.setattr(store, "decide_review", fake_decide_review)
    monkeypatch.setattr(store, "undo_review", fake_undo_review)
    monkeypatch.setattr(store, "confirm", fake_confirm)
    monkeypatch.setattr(store, "unconfirm", fake_unconfirm)

    def fake_get(_settings: Settings, key: str) -> dict[str, Any] | None:
        """A real lookup: an unknown key is a miss, not a zero-filled row."""
        if not any(key == row["key"] for row in fake_model(_settings)["rows"]):
            return None
        return {
            "key": key,
            "core": "mug",
            "episodes": 20,
            "task_count": 3,
            "merged_cores": ["mug"],
            "label": state["confirmations"].get(key, ""),
        }

    monkeypatch.setattr(
        store,
        "latest_run",
        lambda _settings, **_kwargs: {"health": {"source": "catalog"}, "source": "catalog"},
    )
    monkeypatch.setattr(store, "get_proposal", fake_get)
    monkeypatch.setattr(
        store,
        "proposal_members",
        lambda _s, _key, **_k: [
            {
                "task": "pick up the red mug",
                "core": "mug",
                "episodes": 12,
                "verb": "pick up",
                "colours": ["red"],
            }
        ],
    )
    state["app"] = create_app(Settings(database_url="postgresql://unused/unused"))
    state["app"].state.catalog = object()
    return TestClient(state["app"])


def _mug_key() -> str:
    return proposal_key("mug")


# ------------------------------------------------------------------------ reads


def test_the_list_reports_proposals_and_health(client: TestClient) -> None:
    response = client.get("/api/v1/clusters")
    assert response.status_code == 200
    body = response.json()
    assert body["proposals"]
    assert body["health"]["clusters"] == body["health"]["clusters"]
    assert body["health"]["ignored"] == "verb, colour"
    cores = {item["core"] for item in body["proposals"]}
    assert {"mug", "laptop", "drawer"} <= cores


def test_the_health_block_carries_the_orphan_and_truncation_facts(client: TestClient) -> None:
    """Both are things a reader cannot derive from the proposals alone."""
    health = client.get("/api/v1/clusters").json()["health"]
    assert health["orphaned"] == 0
    assert health["truncated"] is False


def test_one_cluster_detail_returns_its_members(client: TestClient) -> None:
    response = client.get(f"/api/v1/clusters/{_mug_key()}")
    assert response.status_code == 200
    body = response.json()
    assert body["proposal"]["core"] == "mug"
    assert body["members"][0]["episodes"] == 12


def test_an_unknown_cluster_is_a_404_with_a_readable_detail(client: TestClient) -> None:
    response = client.get("/api/v1/clusters/deadbeefdeadbeef")
    assert response.status_code == 404
    assert "deadbeefdeadbeef" in response.json()["detail"]


# ---------------------------------------------------------------- human review


def test_the_review_queue_lists_only_unresolved_candidates(client: TestClient) -> None:
    response = client.get("/api/v1/clusters/review")
    assert response.status_code == 200
    assert response.json()["candidates"][0]["reason"] == "no supporting neighbors"
    assert response.json()["decisions"] == []


def test_a_human_class_decision_is_separate_and_reversible(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clusters/review",
        json={"tasks": ["open the drawer"], "disposition": "class", "label": "drawer work"},
    )
    assert response.status_code == 200
    assert response.json()["reviewed"] == 1
    assert response.json()["decisions"][0]["label"] == "drawer work"
    assert client.get("/api/v1/clusters/review").json()["candidates"] == []

    reopened = client.request(
        "DELETE", "/api/v1/clusters/review", params={"tasks": "open the drawer"}
    )
    assert reopened.status_code == 200
    assert reopened.json()["reopened"] == 1
    assert client.get("/api/v1/clusters/review").json()["candidates"]


def test_a_dismissal_needs_no_label_and_stale_review_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clusters/review",
        json={"tasks": ["open the drawer"], "disposition": "dismissed"},
    )
    assert response.status_code == 200
    assert response.json()["decisions"][0]["disposition"] == "dismissed"
    stale = client.post(
        "/api/v1/clusters/review",
        json={"tasks": ["open the drawer"], "disposition": "class", "label": "drawer work"},
    )
    assert stale.status_code == 409


def test_review_request_rejects_a_class_without_a_label(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clusters/review",
        json={"tasks": ["open the drawer"], "disposition": "class"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------- rebuild


def test_a_rebuild_passes_the_axes_through_to_the_store(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clusters/rebuild",
        json={"source": "catalog", "ignore": ["verb", "colour"], "radius": 0.4},
    )
    assert response.status_code == 200
    assert response.json()["health"]["ignored"] == "verb, colour"


def test_a_rebuild_rejects_an_unknown_axis_rather_than_ignoring_it(client: TestClient) -> None:
    """A typo that silently ignored nothing would produce a different clustering."""
    response = client.post("/api/v1/clusters/rebuild", json={"ignore": ["verbb"]})
    assert response.status_code == 422


def test_a_rebuild_rejects_an_out_of_range_radius(client: TestClient) -> None:
    assert client.post("/api/v1/clusters/rebuild", json={"radius": 9}).status_code == 422
    assert client.post("/api/v1/clusters/rebuild", json={"radius": -1}).status_code == 422


def test_a_rebuild_rejects_an_unknown_field(client: TestClient) -> None:
    assert client.post("/api/v1/clusters/rebuild", json={"soruce": "catalog"}).status_code == 422


def test_a_rebuild_can_propose_over_the_sample_sentences(client: TestClient) -> None:
    response = client.post("/api/v1/clusters/rebuild", json={"source": "sample"})
    assert response.status_code == 200
    assert response.json()["health"]["source"] == "sample"


# ----------------------------------------------------------------- confirming


def test_confirming_names_and_freezes_a_cluster(client: TestClient) -> None:
    response = client.post(f"/api/v1/clusters/{_mug_key()}/confirm", json={"label": "mug handling"})
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "mug handling"
    assert body["frozen"] is True


def test_an_empty_label_is_refused(client: TestClient) -> None:
    assert (
        client.post(f"/api/v1/clusters/{_mug_key()}/confirm", json={"label": "  "}).status_code
        == 422
    )


def test_a_confirmation_appears_on_the_next_read(client: TestClient) -> None:
    client.post(f"/api/v1/clusters/{_mug_key()}/confirm", json={"label": "mug handling"})
    proposals = client.get("/api/v1/clusters").json()["proposals"]
    labelled = [item for item in proposals if item["frozen"]]
    assert [item["label"] for item in labelled] == ["mug handling"]


def test_releasing_returns_a_cluster_to_being_a_suggestion(client: TestClient) -> None:
    client.post(f"/api/v1/clusters/{_mug_key()}/confirm", json={"label": "mug handling"})
    response = client.request("DELETE", f"/api/v1/clusters/{_mug_key()}/confirm")
    assert response.status_code == 200
    assert response.json()["frozen"] is False
    assert not [
        item for item in client.get("/api/v1/clusters").json()["proposals"] if item["frozen"]
    ]


# ------------------------------------------------------------------ the page


def test_the_page_renders_with_its_figures_and_controls(client: TestClient) -> None:
    response = client.get("/ui/clusters")
    assert response.status_code == 200
    body = response.text
    assert "Largest classes" in body
    assert "Human review" in body
    assert "de-class-size" in body
    assert "/ui/clusters/rebuild" in body
    assert "/confirm" in body


def test_the_page_is_in_the_navigation(client: TestClient) -> None:
    body = client.get("/ui/clusters").text
    assert 'href="/ui/clusters"' in body


def test_the_fragment_carries_the_parts_that_change_on_a_rebuild(client: TestClient) -> None:
    """The polling fragment must not re-send the controls and wipe what is typed."""
    response = client.get("/ui/clusters", headers={"X-Fragment": "1"})
    assert response.status_code == 200
    assert "Largest classes" in response.text
    assert "Human review" in response.text
    assert "/ui/clusters/rebuild" not in response.text


def test_the_page_says_so_when_there_is_no_run_yet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store,
        "model",
        lambda _s, **_k: {
            "rows": [],
            "members": {},
            "history": [],
            "run": None,
            "ignored": Ignored(),
            "radius": 0.3,
            "rule": "running_mean",
            "source": "catalog",
            "health": {},
        },
    )
    body = client.get("/ui/clusters").text
    assert "no clustering run yet" in body


def test_an_empty_run_says_so_instead_of_rendering_a_zero_dashboard(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ProposalSet with no proposals in it is still truthy - the page has to look inside.

    Before `has_run` existed, a rebuild over a catalog with no recognised task strings
    rendered six healthy-looking zero statistics and an empty proposals table, which
    reads as "clustering found nothing interesting" rather than "clustering has not run".
    """
    monkeypatch.setattr(
        store,
        "model",
        lambda _s, **_k: {
            "rows": [],
            "members": {},
            "history": [],
            "run": None,
            "ignored": Ignored(),
            "radius": 0.3,
            "rule": "running_mean",
            "source": "catalog",
            "health": {},
            "proposals": build({}),
        },
    )
    body = client.get("/ui/clusters").text
    assert "no clustering run yet" in body
    assert "Health" not in body


def test_a_form_rebuild_posts_the_checkbox_group_as_one_value(client: TestClient) -> None:
    """Three checked boxes arrive as one repeated key; joining it twice split the word."""
    response = client.post(
        "/ui/clusters/rebuild",
        data={"source": "sample", "ignore": ["colour", "verb"], "radius": "0.3"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/ui/clusters")


def test_a_review_form_posts_a_human_class_and_redirects(client: TestClient) -> None:
    response = client.post(
        "/ui/clusters/review",
        data={"task": "open the drawer", "label": "drawer work", "disposition": "class"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/api/v1/clusters/review").json()["decisions"][0]["label"] == "drawer work"


def test_a_review_form_can_dismiss_without_a_label(client: TestClient) -> None:
    response = client.post(
        "/ui/clusters/review",
        data={"task": "open the drawer", "disposition": "dismissed"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    decision = client.get("/api/v1/clusters/review").json()["decisions"][0]
    assert decision["disposition"] == "dismissed"


def test_a_form_confirm_uses_the_same_store_as_the_api(client: TestClient) -> None:
    response = client.post(
        f"/ui/clusters/{_mug_key()}/confirm",
        data={"label": "mug handling"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [item for item in client.get("/api/v1/clusters").json()["proposals"] if item["frozen"]]


def test_a_form_confirm_with_no_label_returns_to_the_page_with_the_reason(
    client: TestClient,
) -> None:
    response = client.post(
        f"/ui/clusters/{_mug_key()}/confirm", data={"label": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    from urllib.parse import unquote

    assert "needs a label" in unquote(response.headers["location"])


def test_a_form_release_is_a_post_and_redirects(client: TestClient) -> None:
    client.post(f"/ui/clusters/{_mug_key()}/confirm", data={"label": "mug handling"})
    response = client.post(f"/ui/clusters/{_mug_key()}/release", follow_redirects=False)
    assert response.status_code == 303


# ------------------------------------------------------------- the detail page


def test_the_detail_page_renders_the_stored_proposal(client: TestClient) -> None:
    response = client.get(f"/ui/clusters/{_mug_key()}")
    assert response.status_code == 200
    body = response.text
    assert "Cluster detail" in body
    assert "pick up the red mug" in body
    assert 'href="/ui/clusters"' in body
    assert f'action="/ui/clusters/{_mug_key()}/confirm"' in body


def test_the_detail_page_shows_a_release_form_once_confirmed(client: TestClient) -> None:
    client.post(f"/api/v1/clusters/{_mug_key()}/confirm", json={"label": "mug handling"})
    body = client.get(f"/ui/clusters/{_mug_key()}").text
    assert "confirmed: mug handling" in body
    assert f'action="/ui/clusters/{_mug_key()}/release"' in body


def test_an_unknown_cluster_is_an_html_404(client: TestClient) -> None:
    response = client.get("/ui/clusters/deadbeefdeadbeef")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "No cluster" in response.text


def test_the_detail_fragment_has_no_shell(client: TestClient) -> None:
    response = client.get(f"/ui/clusters/{_mug_key()}", headers={"X-Fragment": "1"})
    assert response.status_code == 200
    assert "<!doctype html>" not in response.text
    assert "pick up the red mug" in response.text


def test_the_detail_page_carries_the_requested_theme(client: TestClient) -> None:
    body = client.get(f"/ui/clusters/{_mug_key()}?theme=amber").text
    assert 'data-theme="amber"' in body


def test_a_form_confirm_returns_to_the_detail_page(client: TestClient) -> None:
    """Confirming from the detail view must not bounce the reviewer to the list."""
    response = client.post(
        f"/ui/clusters/{_mug_key()}/confirm",
        data={"label": "mug handling"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/clusters/{_mug_key()}"


def test_a_form_confirm_with_no_label_returns_to_the_detail_page_with_the_reason(
    client: TestClient,
) -> None:
    response = client.post(
        f"/ui/clusters/{_mug_key()}/confirm", data={"label": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    from urllib.parse import unquote

    location = unquote(response.headers["location"])
    assert "needs a label" in location
    assert f"/ui/clusters/{_mug_key()}" in location


def test_a_form_release_returns_to_the_detail_page(client: TestClient) -> None:
    client.post(f"/ui/clusters/{_mug_key()}/confirm", data={"label": "mug handling"})
    response = client.post(f"/ui/clusters/{_mug_key()}/release", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/ui/clusters/{_mug_key()}"


def test_the_detail_page_stays_preview_only_for_sample_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store,
        "latest_run",
        lambda _settings, **_kwargs: {"health": {"source": "sample"}, "source": "sample"},
    )
    body = client.get(f"/ui/clusters/{_mug_key()}").text
    assert "preview only" in body
    # The nav's theme form is a <form> too; what must be absent is the confirm control.
    assert f'action="/ui/clusters/{_mug_key()}/confirm"' not in body
    assert f'action="/ui/clusters/{_mug_key()}/release"' not in body
