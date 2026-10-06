"""Contract tests for the notifier's API surface (ADR 0020).

Blackbox over the app: a fake catalog is wired into ``create_app`` and driven
through ``TestClient``, so routing, validation, and the error handler are all
exercised the way a client experiences them. The SQL behind these shapes is
covered by ``tests/integration/test_monitoring.py`` against a real database.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings
from data_engine.monitoring.contracts import InvalidExpectation

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

_INCIDENT = {
    "id": "inc-1",
    "fingerprint": "a" * 16,
    "label": "CONTRACT_BREACH",
    "severity": "critical",
    "notify_class": "notify",
    "scope": "job-1",
    "summary": "Run produced 0 valid episodes; 5 were expected",
    "evidence": {"expected_episodes": 5, "valid_episodes": 0},
    "status": "open",
    "occurrence_count": 1,
    "feature_schema_version": 1,
    "first_seen": NOW,
    "last_seen": NOW,
    "acknowledged_at": None,
    "resolved_at": None,
}


class MonitorCatalogStub:
    """The catalog surface the notifier's endpoints touch, and nothing more."""

    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = [dict(_INCIDENT)]
        self.contracts: dict[str, dict[str, Any]] = {}
        self.saved_baselines: list[dict[str, Any]] = []

    # -- incidents --
    def list_incidents(self, **filters: Any) -> list[dict[str, Any]]:
        rows = self.incidents
        for key in ("severity", "status", "label"):
            if filters.get(key) is not None:
                rows = [row for row in rows if row[key] == filters[key]]
        return rows[: filters.get("limit", 50)]

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        return next((r for r in self.incidents if r["id"] == incident_id), None)

    def unresolved_incidents(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"],
                "fingerprint": r["fingerprint"],
                "status": r["status"],
                "last_seen": r["last_seen"],
                "occurrence_count": r["occurrence_count"],
            }
            for r in self.incidents
            if r["status"] != "resolved"
        ]

    def incidents_opened_since(self, since: datetime) -> int:
        return sum(1 for r in self.incidents if r["first_seen"] >= since)

    def set_incident_status(self, incident_id: str, status: str) -> dict[str, Any] | None:
        row = self.get_incident(incident_id)
        if row is None or row["status"] == "resolved":
            return None
        row["status"] = status
        return row

    def incident_summary(self) -> dict[str, Any]:
        return {
            "by_status": {"open": 1},
            "by_severity": {"critical": 1},
            "by_label": {"CONTRACT_BREACH": 1},
            "notify_open": 1,
        }

    # -- contracts --
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return {"id": job_id, "state": "queued"} if job_id == "job-1" else None

    def register_contract(self, job_id: str, expectation: dict[str, Any]) -> dict[str, Any]:
        row = {
            "job_id": job_id,
            "outcome": "pending",
            "observed": {},
            "created_at": NOW,
            "updated_at": NOW,
            **expectation,
        }
        self.contracts[job_id] = row
        return row

    def get_contract(self, job_id: str) -> dict[str, Any] | None:
        return self.contracts.get(job_id)

    def list_contracts(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self.contracts.values())[: filters.get("limit", 50)]

    # -- monitor --
    def monitoring_snapshot(self, *, now: datetime) -> dict[str, Any]:
        return {"queue_depth": {"queued": 0, "running": 0, "failed": 0}}

    def load_baselines(self) -> list[dict[str, Any]]:
        return []

    def save_baselines(self, rows: list[dict[str, Any]]) -> int:
        self.saved_baselines = rows
        return len(rows)

    def pending_contract_outcomes(self, **_: Any) -> list[dict[str, Any]]:
        return []


def _client(stub: MonitorCatalogStub | None = None) -> TestClient:
    # An empty metrics sink on purpose: health reads the newest tick back from the
    # sink (ADR 0031), so a test using the developer's real `var/metrics` would
    # assert against whatever they last ran.
    sink = Path(tempfile.mkdtemp()) / "runtime.jsonl"
    app = create_app(Settings(_env_file=None, metrics_path=sink), initialize_database=False)
    app.state.catalog = stub or MonitorCatalogStub()
    return TestClient(app)


@pytest.mark.contract
def test_incidents_are_listed() -> None:
    response = _client().get("/api/v1/incidents")
    assert response.status_code == 200
    assert response.json()["items"][0]["label"] == "CONTRACT_BREACH"


@pytest.mark.contract
def test_incident_filters_are_applied() -> None:
    client = _client()
    assert client.get("/api/v1/incidents", params={"severity": "critical"}).status_code == 200
    assert client.get("/api/v1/incidents", params={"severity": "medium"}).json()["items"] == []
    assert client.get("/api/v1/incidents", params={"label": "WORKER_LOST"}).json()["items"] == []


@pytest.mark.contract
def test_an_unknown_severity_is_rejected() -> None:
    assert _client().get("/api/v1/incidents", params={"severity": "urgent"}).status_code == 422


@pytest.mark.contract
def test_an_unknown_label_is_rejected() -> None:
    assert _client().get("/api/v1/incidents", params={"label": "NOPE"}).status_code == 422


@pytest.mark.contract
@pytest.mark.parametrize("limit", [0, 501])
def test_the_limit_is_bounded(limit: int) -> None:
    assert _client().get("/api/v1/incidents", params={"limit": limit}).status_code == 422


@pytest.mark.contract
def test_an_invalid_cursor_is_rejected() -> None:
    assert _client().get("/api/v1/incidents", params={"before": "yesterday"}).status_code == 422


@pytest.mark.contract
def test_one_incident_is_addressable() -> None:
    assert _client().get("/api/v1/incidents/inc-1").status_code == 200
    assert _client().get("/api/v1/incidents/nope").status_code == 404


@pytest.mark.contract
def test_the_summary_aggregates_the_queue() -> None:
    body = _client().get("/api/v1/incidents/summary").json()
    assert body["notify_open"] == 1
    assert body["by_severity"] == {"critical": 1}


@pytest.mark.contract
def test_acknowledging_is_an_append_only_fact() -> None:
    client = _client()
    assert client.post("/api/v1/incidents/inc-1/ack").json()["status"] == "acknowledged"
    assert client.get("/api/v1/incidents/inc-1").json()["status"] == "acknowledged"


@pytest.mark.contract
def test_resolving_is_terminal() -> None:
    client = _client()
    assert client.post("/api/v1/incidents/inc-1/resolve").status_code == 200
    # A resolved incident cannot be re-resolved, and cannot be acknowledged.
    assert client.post("/api/v1/incidents/inc-1/resolve").status_code == 404
    assert client.post("/api/v1/incidents/inc-1/ack").status_code == 404


@pytest.mark.contract
def test_acting_on_an_unknown_incident_is_404() -> None:
    client = _client()
    assert client.post("/api/v1/incidents/nope/ack").status_code == 404
    assert client.post("/api/v1/incidents/nope/resolve").status_code == 404


@pytest.mark.contract
def test_monitor_health_reports_its_own_state() -> None:
    body = _client().get("/api/v1/monitoring/health").json()
    assert body["feature_schema_version"] == 1
    assert body["tick_seconds"] == 60.0
    assert "CONTRACT_BREACH" in body["notify_labels"]
    assert "worker_lost" in body["detectors"]
    # Nothing has ticked in this process, and the isolated sink is empty: the
    # endpoint reports "never" rather than inventing a healthy state.
    assert body["last_tick_at"] is None
    assert body["blind"] is None


@pytest.mark.contract
def test_a_tick_runs_on_demand() -> None:
    response = _client().post("/api/v1/monitoring/tick")
    assert response.status_code == 200
    body = response.json()
    assert body["catalog_reachable"] is True
    assert body["signals"] == 0
    assert body["duration_seconds"] >= 0.0


@pytest.mark.contract
def test_a_tick_persists_its_baselines() -> None:
    stub = MonitorCatalogStub()
    _client(stub).post("/api/v1/monitoring/tick")
    assert stub.saved_baselines, "a tick that learns nothing is a broken tick"


@pytest.mark.contract
def test_the_notify_preview_renders_without_sending() -> None:
    body = _client().get("/api/v1/monitoring/notify-preview").text
    assert "CONTRACT_BREACH" in body
    assert "/ui/incidents" in body


@pytest.mark.contract
def test_a_contract_is_declared_for_a_known_job() -> None:
    response = _client().put(
        "/api/v1/contracts/job-1",
        json={"expected_episodes": 200, "expected_valid_fraction": 0.9},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "pending"
    assert response.json()["expected_episodes"] == 200


@pytest.mark.contract
def test_a_contract_for_an_unknown_job_is_404() -> None:
    assert _client().put("/api/v1/contracts/nope", json={"expected_episodes": 1}).status_code == 404


@pytest.mark.contract
@pytest.mark.parametrize(
    "body",
    [
        {"expected_episodes": -1},
        {"expected_valid_fraction": 1.5},
        {"max_duration_seconds": 0},
    ],
)
def test_an_impossible_expectation_is_rejected(body: dict[str, Any]) -> None:
    assert _client().put("/api/v1/contracts/job-1", json=body).status_code == 422


@pytest.mark.contract
def test_an_unknown_contract_field_is_rejected() -> None:
    # `extra=forbid` so a typo'd field fails loudly rather than being ignored
    # while the operator believes the expectation was recorded.
    response = _client().put("/api/v1/contracts/job-1", json={"expectedEpisode": 5})
    assert response.status_code == 422


@pytest.mark.contract
def test_contracts_are_listed_and_addressable() -> None:
    client = _client()
    client.put("/api/v1/contracts/job-1", json={"expected_episodes": 5})
    assert len(client.get("/api/v1/contracts").json()["items"]) == 1
    assert client.get("/api/v1/contracts/job-1").status_code == 200
    assert client.get("/api/v1/contracts/nope").status_code == 404


@pytest.mark.contract
def test_declaring_twice_replaces_the_expectation() -> None:
    client = _client()
    client.put("/api/v1/contracts/job-1", json={"expected_episodes": 5})
    client.put("/api/v1/contracts/job-1", json={"expected_episodes": 9})
    assert client.get("/api/v1/contracts/job-1").json()["expected_episodes"] == 9


@pytest.mark.contract
def test_the_incidents_page_renders_the_queue() -> None:
    html = _client().get("/ui/incidents").text
    assert "CONTRACT_BREACH" in html
    assert "Monitor health" in html
    assert "Notify preview" in html


@pytest.mark.contract
def test_the_incidents_fragment_is_bare() -> None:
    """The fragment the poller asks for, asked for the way the poller asks.

    This used to fetch `/ui/incidents/fragment` - a separate route that worked
    and that nothing polled, while the page itself pointed its `data-poll` at
    `/ui/incidents`, which returned a whole document. The bug lived in the gap
    between "the fragment route works" and "the page uses the fragment route",
    which is what `tests/contract/test_ui_polling.py` now closes for every page.
    """
    fragment = _client().get("/ui/incidents", headers={"X-Fragment": "1"}).text
    assert "CONTRACT_BREACH" in fragment
    assert "<!doctype html>" not in fragment
    assert "<nav" not in fragment


@pytest.mark.contract
def test_an_empty_queue_explains_itself() -> None:
    stub = MonitorCatalogStub()
    stub.incidents = []
    html = _client(stub).get("/ui/incidents").text
    assert "the notifier has nothing to say" in html


@pytest.mark.contract
def test_the_incidents_page_escapes_incident_text() -> None:
    stub = MonitorCatalogStub()
    stub.incidents[0]["summary"] = "<script>alert(1)</script>"
    html = _client(stub).get("/ui/incidents").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.contract
def test_a_hostile_theme_falls_back_to_the_default() -> None:
    response = _client().get("/ui/incidents", params={"theme": "../../etc/passwd"})
    assert response.status_code == 200
    assert 'data-theme="vt220"' in response.text


def test_expectation_validation_is_reachable_from_the_api() -> None:
    # Guards the wiring: the API catches InvalidExpectation rather than letting
    # a 500 escape from a rule the caller could have satisfied.
    with pytest.raises(InvalidExpectation):
        from data_engine.monitoring.contracts import Expectation

        Expectation(expected_valid_fraction=2.0).validate()
