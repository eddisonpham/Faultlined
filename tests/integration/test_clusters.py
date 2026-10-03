"""Integration tests for cluster proposal storage (real SQL).

The contract tests in `tests/contract/test_cluster_api.py` replace this module, so the
SQL underneath them is only ever asserted here: the four tables, the transactional
rebuild, and the one property the whole design exists for - a confirmation survives a
rebuild that re-keys every proposal.

That property was measured broken. The first version pointed
`cluster_confirmations` at `task_clusters(key)` with `ON DELETE CASCADE`, and one
rebuild with a different axis set deleted every label an operator had written. The
tests below are written so that version would fail them.

`replace_proposals` truncates the proposal tables by design, so nothing here is safe to
run against a catalog whose cluster state matters, and each test removes the
confirmations it wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from data_engine.catalog import clusters as store
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.clustering import Ignored, build
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn

pytestmark = pytest.mark.integration

#: A fixed corpus with one obvious group (the mugs), so what is being asserted is the
#: storage and not a grouping decision the clustering layer already owns in its own tests.
CONTROLLED = {
    "put the red mug on the plate": 12,
    "put the blue mug on the plate": 8,
    "put the green mug on the plate": 5,
    "put the mug on the plate": 3,
    "put the laptop on the plate": 4,
    "open the drawer": 7,
    "wipe the counter": 2,
}


@pytest.fixture
def settings() -> Iterator[Settings]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    configured = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(configured)
    yield configured


@pytest.fixture
def catalog(settings: Settings) -> PostgresCatalog:
    return PostgresCatalog(settings)


@pytest.fixture
def seed(catalog: PostgresCatalog) -> str:
    """Three mug task strings in the catalog, isolated from anything else by a token."""
    token = uuid.uuid4().hex[:8]
    for colour in ("red", "blue", "green"):
        episode_token = f"{token}-{colour}"
        job, _ = catalog.submit_job("ingest", {}, f"clusters-{episode_token}", "test-correlation")
        catalog.register_episode(
            source_hash=episode_token,
            artifact_hash=episode_token,
            size_bytes=512,
            metadata={"task": f"pick up the {colour} mug {token}", "robot": "so101"},
            job_id=str(job["id"]),
            episode_key=f"episode_{episode_token}",
            episode_format="lerobot-v3",
        )
    return token


@pytest.fixture
def cleanup(settings: Settings) -> Iterator[None]:
    """Remove every confirmation this test wrote, whatever it wrote it under."""
    yield
    with store.connect(settings) as connection:
        connection.execute("DELETE FROM cluster_confirmations WHERE label LIKE 'pytest-%%'")


def _label() -> str:
    return f"pytest-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------ the rebuild


def test_a_rebuild_writes_proposals_a_run_and_a_health_block(settings: Settings) -> None:
    result = store.rebuild(settings, source="sample")
    assert result["run_id"] > 0
    assert result["proposals"].proposals

    rows = store.list_proposals(settings)
    assert len(rows) == len(result["proposals"].proposals)
    assert rows[0]["episodes"] >= rows[-1]["episodes"], "the biggest cluster should be first"

    run = store.latest_run(settings)
    assert run is not None
    assert run["source"] == "sample"
    assert run["health"]["clusters"] == len(result["proposals"].proposals)


def test_a_second_rebuild_replaces_the_first_rather_than_adding_to_it(
    settings: Settings,
) -> None:
    # Establish a baseline run of our own so the assertions below do not depend on
    # what an earlier test left behind. run_history is capped and this database is
    # never truncated, so counting rows stopped growing once the cap was reached:
    # the original `len(run_history(limit=200)) == before + 2` passed for dozens of
    # runs and then failed on its own, with no code change, at 247 accumulated runs.
    store.rebuild(settings, source="sample", ignored="site")
    before = [row["id"] for row in store.run_history(settings, limit=5)]

    store.rebuild(settings, source="sample", ignored="colour")
    first = len(store.list_proposals(settings))
    store.rebuild(settings, source="sample", ignored="verb,colour")
    assert len(store.list_proposals(settings)) != first, "a different axis set should regroup"

    after = store.run_history(settings, limit=5)
    new = [row["id"] for row in after if row["id"] not in before]
    assert len(new) == 2, "each rebuild appends one run rather than rewriting the last"
    assert before[0] in [row["id"] for row in after], "the earlier run survives the rebuild"


def test_the_page_model_keeps_run_history_when_proposals_are_empty(settings: Settings) -> None:
    """Removing current proposals must not falsify the persisted run history."""
    result = store.rebuild(settings, source="sample")
    with store.connect(settings) as connection:
        connection.execute("DELETE FROM task_cluster_members")
        connection.execute("DELETE FROM task_clusters")
    empty = store.model(settings)
    assert empty["rows"] == []
    assert empty["run"] is not None
    assert empty["run"]["source"] == "sample"
    assert empty["health"]["clusters"] == result["health"]["clusters"]


def test_the_page_model_does_not_recompute_what_the_run_stored(settings: Settings) -> None:
    """A page load must not change the numbers it is showing."""
    result = store.rebuild(settings, source="sample", ignored="colour")
    before = store.model(settings)
    after = store.model(settings)
    assert before["rows"] == after["rows"]
    assert after["run"] is not None
    assert after["run"]["health"]["clusters"] == len(result["proposals"].proposals)


def test_the_catalog_source_counts_each_task_string_once(settings: Settings, seed: str) -> None:
    tasks = store.distinct_tasks(settings)
    assert sum(count for task, count in tasks.items() if seed in task) == 3
    result = store.rebuild(settings)
    assert result["proposals"].proposals
    assert result["health"]["tasks"] == len(tasks)
    assert result["health"]["truncated"] is False


def test_the_members_of_a_cluster_read_back_biggest_first(settings: Settings) -> None:
    store.replace_proposals(settings, build(CONTROLLED, ignored=Ignored(verb=True, colour=True)))
    key = max(
        (row for row in store.list_proposals(settings)),
        key=lambda row: row["task_count"],
    )["key"]
    members = store.proposal_members(settings, key)
    assert len(members) == max(row["task_count"] for row in store.list_proposals(settings))
    episodes = [int(member["episodes"]) for member in members]
    assert episodes == sorted(episodes, reverse=True)
    assert all("task" in member and "core" in member for member in members)


def test_a_rebuild_reads_the_catalog_and_reports_the_truncation_flag(
    settings: Settings, seed: str
) -> None:
    tasks = store.distinct_tasks(settings)
    assert sum(count for task, count in tasks.items() if seed in task) == 3
    result = store.rebuild(settings, limit=1)
    assert result["health"]["tasks"] == 1
    assert result["health"]["truncated"] is True, "a capped read has to say it was capped"


# -------------------------------------------------------------- confirmations


def test_confirming_names_a_proposal_and_the_name_reads_back(
    settings: Settings, cleanup: None
) -> None:
    result = store.rebuild(settings, source="sample")
    key = result["proposals"].ordered()[0].key
    label = _label()
    assert store.confirm(settings, key, label) is True

    detail = store.get_proposal(settings, key)
    assert detail is not None
    assert detail["label"] == label
    assert label in [row["label"] for row in store.list_proposals(settings)]
    assert store.get_proposal(settings, "no-such-cluster") is None


def test_a_confirmation_cannot_be_recorded_for_nothing(settings: Settings) -> None:
    store.rebuild(settings, source="sample")
    assert store.confirm(settings, "no-such-cluster", _label()) is False
    with pytest.raises(ValueError, match="needs a label"):
        store.confirm(settings, "no-such-cluster", "   ")


def test_releasing_a_confirmation_puts_the_cluster_back_in_play(
    settings: Settings, cleanup: None
) -> None:
    result = store.rebuild(settings, source="sample")
    key = result["proposals"].ordered()[0].key
    store.confirm(settings, key, _label())
    assert store.unconfirm(settings, key) is True
    detail = store.get_proposal(settings, key)
    assert detail is not None and detail["label"] == ""
    assert store.unconfirm(settings, key) is False


def test_a_confirmation_survives_a_rebuild_that_re_keys_every_proposal(
    settings: Settings, cleanup: None
) -> None:
    """The property the whole module exists for.

    Changing the ignored axes changes every core, so every proposal key changes. The
    confirmation is stored as the task strings it covers and has to find them again.

    Written over a controlled proposal set rather than over the catalog on purpose: the
    test database is shared, and grouping a few thousand unrelated task strings is the
    clustering's business, not the store's. What is under test here is that a
    confirmation written against one set of rows is found again in another.
    """
    first = build(CONTROLLED, ignored=Ignored(verb=True, colour=True))
    key = max(first.proposals, key=lambda item: item.task_count).key
    label = _label()
    store.replace_proposals(settings, first)
    assert store.confirm(settings, key, label) is True

    # Ignoring the site as well takes the core from "mug on plate" to "mug": a different
    # key, holding the same four task strings.
    second = build(CONTROLLED, ignored=Ignored(verb=True, colour=True, site=True))
    assert max(second.proposals, key=lambda item: item.task_count).key != key, (
        "if the key did not change, this proves nothing"
    )
    result = store.replace_proposals(settings, second)
    assert result["frozen"] == 1
    assert result["orphaned"] == 0

    carried = [row for row in store.list_proposals(settings) if row["label"] == label]
    assert len(carried) == 1
    detail = store.get_proposal(settings, carried[0]["key"])
    assert detail is not None and detail["label"] == label


def test_a_confirmation_whose_strings_are_gone_is_counted_as_orphaned(
    settings: Settings, cleanup: None
) -> None:
    """Reported, never quietly reattached to the nearest surviving cluster.

    Shrinking the radius scatters the confirmed group. Every remaining fragment still
    shares a task string with the confirmation, so matching on *any* overlap would put
    the operator's name on an arbitrary neighbour. It is orphaned instead, and the claim
    stays in the table where the operator can find it.
    """
    result = store.rebuild(settings, source="sample")
    group = max(
        (item for item in result["proposals"].proposals if item.task_count > 1),
        key=lambda item: item.task_count,
    )
    label = _label()
    store.confirm(settings, group.key, label)

    rebuilt = store.rebuild(settings, source="sample", radius=0.001)
    assert rebuilt["orphaned"] == 1
    assert rebuilt["frozen"] == 0
    assert not [row for row in store.list_proposals(settings) if row["label"] == label]
    assert label in [entry.label for entry in store.confirmations(settings)], (
        "an orphaned confirmation is still the operator's claim, and is still stored"
    )


def test_review_decision_is_persisted_filtered_and_reopened(settings: Settings) -> None:
    """Review state is independent of automatic proposals and confirmation labels."""
    token = uuid.uuid4().hex
    key = f"review-{token}"
    selected = f"!review-{token}"
    with store.connect(settings) as connection, connection.transaction():
        confirmation_count = connection.execute(
            "SELECT count(*) AS n FROM cluster_confirmations"
        ).fetchone()["n"]
        connection.execute(
            """INSERT INTO task_clusters (key, core, episodes, task_count, source, radius)
               VALUES (%s, 'unmatched review core', 1, 1, 'catalog', 0.3)""",
            (key,),
        )
        connection.execute(
            """INSERT INTO task_cluster_members
                       (cluster_key, task, core, episodes)
               VALUES (%s, %s, 'unmatched review core', 0)""",
            (key, selected),
        )

    try:
        assert selected in {item["task"] for item in store.review_candidates(settings)}
        assert (
            store.decide_review(settings, [selected], disposition="class", label="operator class")
            == 1
        )
        assert selected not in {item["task"] for item in store.review_candidates(settings)}
        with store.connect(settings) as connection:
            decision = connection.execute(
                """SELECT task, disposition, label FROM cluster_review_decisions
                   WHERE task = %s""",
                (selected,),
            ).fetchone()
        assert decision == {
            "task": selected,
            "disposition": "class",
            "label": "operator class",
        }
        assert store.undo_review(settings, [selected]) == 1
        assert selected in {item["task"] for item in store.review_candidates(settings)}
        with store.connect(settings) as connection:
            assert (
                connection.execute("SELECT count(*) AS n FROM cluster_confirmations").fetchone()[
                    "n"
                ]
                == confirmation_count
            )
    finally:
        with store.connect(settings) as connection, connection.transaction():
            connection.execute("DELETE FROM cluster_review_decisions WHERE task = %s", (selected,))
            connection.execute("DELETE FROM task_cluster_members WHERE cluster_key = %s", (key,))
            connection.execute("DELETE FROM task_clusters WHERE key = %s", (key,))


def test_review_rejects_non_candidates_and_unbounded_actions(settings: Settings) -> None:
    with pytest.raises(ValueError, match="between 1 and 25"):
        store.decide_review(settings, [], disposition="dismissed")
    with pytest.raises(ValueError, match="between 1 and 25"):
        store.undo_review(settings, [f"task-{index}" for index in range(26)])
    with pytest.raises(ValueError, match="no longer in the candidate queue"):
        store.decide_review(settings, ["not a current candidate"], disposition="dismissed")


def test_confirming_the_same_group_twice_keeps_one_confirmation(
    settings: Settings, cleanup: None
) -> None:
    result = store.rebuild(settings, source="sample")
    key = result["proposals"].ordered()[0].key
    label = _label()
    store.confirm(settings, key, label)
    store.confirm(settings, key, f"{label}-renamed")
    assert len(store.confirmations(settings)) == 1
    detail = store.get_proposal(settings, key)
    assert detail is not None and detail["label"] == f"{label}-renamed"


# ------------------------------------------------------------- the detail view


def _ui_client(settings: Settings) -> object:
    """The real app over the real catalog, driven only through HTTP."""
    from fastapi.testclient import TestClient

    from data_engine.api.app import create_app

    app = create_app(
        Settings(_env_file=None, database_url=settings.database_url),  # type: ignore[arg-type]
        initialize_database=False,
    )
    return TestClient(app)


def _mug_proposal(settings: Settings) -> dict[str, Any]:
    store.replace_proposals(settings, build(CONTROLLED, ignored=Ignored(verb=True, colour=True)))
    row = max(
        (row for row in store.list_proposals(settings)),
        key=lambda row: row["task_count"],
    )
    assert "mug" in row["core"]
    return row


def test_the_detail_page_serves_the_stored_proposal_and_its_members(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    row = _mug_proposal(settings)
    client = _ui_client(settings)
    assert isinstance(client, TestClient)
    response = client.get(f"/ui/clusters/{row['key']}")
    assert response.status_code == 200
    body = response.text
    assert "Cluster detail" in body
    assert 'href="/ui/clusters"' in body
    # The stored member task strings, with their extraction facts, are on the page.
    assert "put the red mug on the plate" in body
    assert "put" in body
    assert "red" in body
    # The tile counts agree with the catalog, and the confirm control is present.
    assert f'<span class="de-stat-value">{row["episodes"]}</span>' in body
    assert f'action="/ui/clusters/{row["key"]}/confirm"' in body


def test_an_unknown_cluster_is_an_html_404_over_the_real_app(settings: Settings) -> None:
    from fastapi.testclient import TestClient

    client = _ui_client(settings)
    assert isinstance(client, TestClient)
    response = client.get("/ui/clusters/no-such-cluster")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_confirm_and_release_round_trip_through_the_detail_page(
    settings: Settings, cleanup: None
) -> None:
    from fastapi.testclient import TestClient

    row = _mug_proposal(settings)
    client = _ui_client(settings)
    assert isinstance(client, TestClient)
    label = _label()

    confirm = client.post(
        f"/ui/clusters/{row['key']}/confirm", data={"label": label}, follow_redirects=False
    )
    assert confirm.status_code == 303
    assert confirm.headers["location"] == f"/ui/clusters/{row['key']}"

    page = client.get(f"/ui/clusters/{row['key']}").text
    assert f"confirmed: {label}" in page
    assert "release confirmation" in page

    release = client.post(f"/ui/clusters/{row['key']}/release", follow_redirects=False)
    assert release.status_code == 303
    assert release.headers["location"] == f"/ui/clusters/{row['key']}"

    page = client.get(f"/ui/clusters/{row['key']}").text
    assert "confirmed:" not in page
    assert "/confirm" in page


def test_the_detail_page_holds_the_extraction_facts_the_catalog_stores(
    settings: Settings,
) -> None:
    """The member rows come from SQL, so the facts on the page must be the stored ones."""
    from fastapi.testclient import TestClient

    row = _mug_proposal(settings)
    stored = store.proposal_members(settings, row["key"])
    assert all({"task", "core", "episodes", "verb", "colours"} <= set(m) for m in stored)

    client = _ui_client(settings)
    assert isinstance(client, TestClient)
    body = client.get(f"/ui/clusters/{row['key']}").text
    for member in stored:
        assert str(member["task"]) in body
