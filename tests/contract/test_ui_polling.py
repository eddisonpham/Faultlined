"""Every polled page must answer its own poll target with a bare fragment."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings

POLLED = (
    "/ui",
    "/ui/jobs",
    "/ui/episodes",
    "/ui/insights",
    "/ui/metrics",
    "/ui/failures",
    "/ui/slices",
    "/ui/incidents",
    "/ui/artifacts",
)


class _EmptyCatalog:
    """The smallest catalog surface that lets every page render."""

    def count_jobs_by_state(self) -> dict[str, int]:
        return {}

    def __getattr__(self, name: str) -> Any:
        def _empty(*_args: Any, **_kwargs: Any) -> Any:
            if name.startswith(("count_", "queue_")):
                return 0
            if name.startswith("get_"):
                return None
            if name.endswith(("summary", "snapshot", "report", "manifest")):
                return {}
            if name.startswith("list_") or name.endswith(("_episodes", "s")):
                return []
            return 0

        return _empty


def _client() -> TestClient:
    app = create_app(Settings(_env_file=None), initialize_database=False)
    app.state.catalog = _EmptyCatalog()
    return TestClient(app)


@pytest.mark.contract
@pytest.mark.parametrize("path", POLLED)
def test_a_polled_page_answers_its_own_poll_target_with_a_fragment(path: str) -> None:
    client = _client()
    page = client.get(path).text
    targets = re.findall(r'data-poll="([^"]+)"', page)
    assert targets, f"{path} declares no live region; is that intended?"

    for target in targets:
        body = client.get(target, headers={"X-Fragment": "1"}).text
        assert "<!doctype html>" not in body.lower(), (
            f"{path} polls {target}, and that target returned a whole document. "
            "The poller will nest the entire page - nav bar included - inside "
            "the panel it is meant to replace."
        )
        assert "<nav" not in body, f"{path} polls {target}, which returned a nav bar"
        assert "</html>" not in body.lower(), f"{path} polls {target}, which closed the document"


@pytest.mark.contract
def test_a_page_never_polls_a_url_that_only_exists_to_be_a_fragment() -> None:
    """The orphaned route is gone, so nothing can start depending on it again."""
    assert _client().get("/ui/incidents/fragment").status_code == 404
