"""Every polled page must answer its own poll target with a bare fragment.

This test exists because of a bug a human found by looking at the screen, not
because of anything the source said was wrong.

`/ui/incidents` carried `data-poll="/ui/incidents"`, and that route ignored the
`X-Fragment` header and always returned a whole document. The poller replaced
the panel with that document, so the page rendered a second nav bar and the
whole UI nested inside one panel - after the first ten-second tick, and only
then, which is why it looked like a rendering glitch rather than a routing one.

There *was* a test, and it passed: `/ui/incidents/fragment` existed, returned a
bare fragment, and was asserted to be bare. But nothing polled that URL. The
test proved the fragment route worked, not that the page used it, and the gap
between those two claims is exactly where the bug lived.

So this asserts the whole chain, for every page that polls: the page declares a
poll target, and asking that target the way the poller asks returns a fragment
rather than a document.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings

#: Every page that declares a live region. Kept explicit rather than discovered,
#: because the point is to fail loudly when someone adds a polling page and
#: forgets the convention - a discovered list cannot notice an omission.
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
    """The smallest catalog surface that lets every page render.

    Deliberately returns nothing: a page whose poll target is a document is
    broken whether or not it has data, and an empty catalog keeps this test
    about the routing rather than about fixtures.
    """

    def count_jobs_by_state(self) -> dict[str, int]:
        # Not the name rules below: they answer `count_*` with 0, and this
        # method's contract is a mapping (states -> depths), not a scalar.
        return {}

    def __getattr__(self, name: str) -> Any:
        def _empty(*_args: Any, **_kwargs: Any) -> Any:
            # Order is load-bearing: count_artifacts() ends in "s", so the
            # plural rule must never be consulted before the numeric one.
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
    """The orphaned route is gone, so nothing can start depending on it again.

    It worked, it was tested, and nothing used it. Leaving it in place is how
    this same bug gets reintroduced in a year.
    """
    assert _client().get("/ui/incidents/fragment").status_code == 404
