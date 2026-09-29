"""Contract tests for the episode-quality and quality-summary endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings

_QUALITY: dict[str, Any] = {
    "episode_id": "episode-1",
    "frame_count": 303,
    "movement_score": 0.042,
    "jerk_score": 0.011,
    "stall_ratio": 0.07,
    "verdict": "smooth",
    "dims": [
        {
            "name": "action[0]",
            "active": True,
            "discrete": False,
            "gripper": False,
            "norm_delta_std": 0.02,
            "mean_abs_delta_norm": 0.01,
        }
    ],
    "length_zscore": 0.5,
    "computed_at": "2026-09-29T00:00:00Z",
}

_SUMMARY: dict[str, Any] = {
    "episode_count": 2,
    "verdicts": {"smooth": 2},
    "length": {"count": 2, "mean": 250.0, "std": 70.0, "min": 200, "max": 300, "histogram": []},
    "speed_distribution": [
        {"episode_id": "episode-1", "movement_score": 0.04, "verdict": "smooth"}
    ],
    "heat_matrix": {
        "dims": ["action[0]"],
        "episodes": [{"episode_id": "episode-1", "values": [0.02]}],
    },
    "outliers": {"jerk": [], "stall": [], "length": []},
}


class FakeCatalog:
    def get_episode_quality(self, episode_id: str) -> dict[str, Any] | None:
        return dict(_QUALITY) if episode_id == "episode-1" else None

    def quality_summary(self) -> dict[str, Any]:
        return _SUMMARY


def _client() -> TestClient:
    app = create_app(
        Settings(_env_file=None),
        initialize_database=False,
        catalog=FakeCatalog(),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.mark.contract
def test_episode_quality_endpoint_returns_the_signal_contract() -> None:
    response = _client().get("/api/v1/episodes/episode-1/quality")
    assert response.status_code == 200
    body = response.json()
    assert body["episode_id"] == "episode-1"
    assert body["verdict"] == "smooth"
    assert body["movement_score"] == pytest.approx(0.042)
    assert body["length_zscore"] == pytest.approx(0.5)
    assert body["dims"][0]["name"] == "action[0]"
    assert set(body["dims"][0]) >= {"active", "discrete", "gripper", "norm_delta_std"}


@pytest.mark.contract
def test_episode_quality_endpoint_404s_like_every_other_resource() -> None:
    response = _client().get("/api/v1/episodes/nope/quality")
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.contract
def test_quality_summary_returns_distributions_and_outliers() -> None:
    response = _client().get("/api/v1/quality/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["episode_count"] == 2
    assert body["verdicts"] == {"smooth": 2}
    assert body["length"]["count"] == 2
    assert body["speed_distribution"][0]["episode_id"] == "episode-1"
    assert body["heat_matrix"]["dims"] == ["action[0]"]
    assert set(body["outliers"]) == {"jerk", "stall", "length"}
