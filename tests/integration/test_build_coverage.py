"""Build coverage over a real catalog (ADR 0033).

The unit tests hand `coverage_report` rows that are shaped the way the query promises. This is the
round trip that can break: four persisted attributes are unpivoted, joined against the catalog the
build was drawn from, and the task axis's gaps are read from the vocabulary rather than from the
episodes. It also pins the two things the first real run got wrong - `sum()` over a `bigint` comes
back as `numeric`, so every total has to be cast and read as a count - and the property the ADR
argues for: the report is computed on read, so it follows the catalog without touching the build.

The suite shares one database and does not truncate between tests, so the assertions are written for
a catalog that already holds generations of earlier runs and will hold this one's rows tomorrow:
counts about the catalog are relative (a baseline plus what this test seeded), gaps are asserted by
membership in a name this test owns, and the probe rows read with a window wide enough that the
shared catalog cannot hide one of them behind an axis cap. The caps themselves are exercised by the
boundedness test, which asserts on its own build rather than on the catalog. A first version used
fixed probe names and read through the shipped caps; it passed alone and failed in the full suite,
which is exactly the failure this discipline prevents.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from data_engine.analysis.coverage import coverage_report
from data_engine.analysis.quality import analyze
from data_engine.builds.service import DatasetBuilder
from data_engine.catalog import vocabulary
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn

pytestmark = pytest.mark.integration

#: Names this test owns, so a gap assertion cannot be satisfied or broken by another test's data.
PICK = "coverage-probe pick"
FOLD = "coverage-probe fold"
UNFILLED = "coverage-probe place the can"
PICK_TASK = "coverage-probe/pick the cube"
FOLD_TASK = "coverage-probe/fold the cloth"

#: The read window the gap assertions use. The route asks for `max(VALUE_LIMIT, GAP_LIMIT)`; a test
#: that asserts a gap list asks for more, so nothing another test left in the catalog can push this
#: test's own value past the cap. The shipped caps are what the boundedness test is about.
WIDE = 500


@pytest.fixture
def settings() -> Iterator[Settings]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    settings = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(settings)
    yield settings


@pytest.fixture
def catalog(settings: Settings) -> PostgresCatalog:
    return PostgresCatalog(settings)


def _seed(catalog: PostgresCatalog, *, task: str, robot: str, scored: bool = True) -> str:
    """Register one episode the way ingest does, and optionally score it."""
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job("ingest", {}, f"coverage-{token}", "test-correlation")
    episode = catalog.register_episode(
        source_hash=token,
        artifact_hash=token,
        size_bytes=256,
        metadata={"task": task, "robot": robot},
        job_id=str(job["id"]),
        episode_key=f"episode_{token[:8]}",
        episode_format="synthetic-json",
    )
    if scored:
        catalog.record_episode_quality(
            str(episode["id"]),
            analyze({"arm": [0.0, 0.1, 0.2, 0.3]}, timestamps=[0.0, 0.05, 0.1, 0.15]).to_dict(),
        )
    return str(episode["id"])


def _build(catalog: PostgresCatalog, episode_ids: list[str], name: str) -> str:
    result = DatasetBuilder(code_commit="testcoverage1").build(
        catalog.get_episodes(episode_ids), name=name
    )
    catalog.record_build(result, job_id="job-build-coverage")
    return result.hash


def _report(catalog: PostgresCatalog, build_hash: str) -> dict:
    """The report as a wide read, so a gap assertion cannot be defeated by the axis cap."""
    inputs = catalog.build_coverage_inputs(build_hash, limit=WIDE)
    return coverage_report(build_hash, inputs, gap_limit=WIDE).to_dict()


def _axis(report: dict, name: str) -> dict:
    return next(axis for axis in report["axes"] if axis["axis"] == name)


def test_coverage_round_trips_a_real_build_against_the_catalog_it_came_from(
    catalog: PostgresCatalog,
) -> None:
    baseline = catalog.count_episodes()
    selected = [
        _seed(catalog, task=PICK_TASK, robot="so101"),
        _seed(catalog, task=PICK_TASK, robot="so101"),
        _seed(catalog, task=FOLD_TASK, robot="ur5e"),
        _seed(catalog, task=FOLD_TASK, robot="ur5e"),
    ]
    build_hash = _build(catalog, selected[:2], "coverage-probe-pick")

    inputs = catalog.build_coverage_inputs(build_hash)
    report = coverage_report(build_hash, inputs).to_dict()

    assert inputs["episode_count"] == 2
    assert isinstance(inputs["episode_count"], int), "sum(bigint) is numeric; it has to be cast"
    assert isinstance(inputs["catalog_size"], int)
    assert report["episode_count"] == 2
    assert report["catalog_size"] == baseline + 4
    assert report["coverage_ratio"] == pytest.approx(2 / (baseline + 4))

    task = _axis(report, "task")
    robot = _axis(report, "robot")
    assert task["present"] == 1
    assert task["values"][0]["count"] == 2
    assert task["values"][0]["share"] == 1.0
    assert task["values"][0]["value"] not in task["gaps"], "a value the build holds is not a gap"
    assert "ur5e" in robot["gaps"], "the catalog holds another embodiment; the build left it out"
    assert "so101" not in robot["gaps"]
    # Other tests share the database and may have ingested other formats, so this is a membership
    # check rather than an empty list.
    assert "synthetic-json" not in _axis(report, "format")["gaps"]


def test_a_vocabulary_label_with_no_episode_anywhere_is_still_a_gap(
    settings: Settings, catalog: PostgresCatalog
) -> None:
    """The task reference is what the operator named, not what happens to be stored."""
    pick = vocabulary.create_entry(settings, preferred_label=PICK)
    fold = vocabulary.create_entry(settings, preferred_label=FOLD)
    vocabulary.create_entry(settings, preferred_label=UNFILLED)
    vocabulary.map_task(settings, task_string=PICK_TASK, entry_id=pick["id"])
    vocabulary.map_task(settings, task_string=FOLD_TASK, entry_id=fold["id"])
    selected = [
        _seed(catalog, task=PICK_TASK, robot="so101"),
        _seed(catalog, task=PICK_TASK, robot="so101"),
        _seed(catalog, task=FOLD_TASK, robot="so101"),
    ]

    subset = _report(catalog, _build(catalog, selected[:2], "coverage-probe-vocabulary"))
    whole = _report(catalog, _build(catalog, selected, "coverage-probe-whole"))

    subset_task = _axis(subset, "task")
    # The raw task strings normalise through the vocabulary on the way out.
    assert [item["value"] for item in subset_task["values"]] == [PICK]
    assert FOLD in subset_task["gaps"], "a label with an episode elsewhere is a gap here"
    assert UNFILLED in subset_task["gaps"], "a label with no episode anywhere is still a gap"
    assert PICK not in subset_task["gaps"]
    assert subset["vocabulary_missing"] >= 2

    whole_task = _axis(whole, "task")
    assert [item["value"] for item in whole_task["values"]] == [PICK, FOLD]
    assert UNFILLED in whole_task["gaps"], "the whole catalog still lacks the named task"
    assert FOLD not in whole_task["gaps"]
    assert whole["coverage_ratio"] >= 3 / whole["catalog_size"]


def test_an_unscored_episode_is_its_own_verdict_value_rather_than_a_missing_row(
    catalog: PostgresCatalog,
) -> None:
    selected = [
        _seed(catalog, task="coverage-probe/verdict", robot="so101"),
        _seed(catalog, task="coverage-probe/verdict", robot="so101", scored=False),
    ]
    report = _report(catalog, _build(catalog, selected, "coverage-probe-verdict"))

    verdict = _axis(report, "verdict")
    values = [item["value"] for item in verdict["values"]]
    assert "(unscored)" in values
    assert len(values) == 2, "the scored episode is still counted on its own verdict"
    assert sum(item["count"] for item in verdict["values"]) == 2


def test_the_report_is_bounded_by_the_axes_rather_than_by_the_episode_count(
    catalog: PostgresCatalog,
) -> None:
    selected = [
        _seed(catalog, task=f"coverage-probe/task {index:02d}", robot="so101")
        for index in range(25)
    ]
    build_hash = _build(catalog, selected, "coverage-probe-wide")

    inputs = catalog.build_coverage_inputs(build_hash, limit=20)
    report = coverage_report(build_hash, inputs).to_dict()
    task = _axis(report, "task")

    assert report["episode_count"] == 25
    assert task["present"] == 25, "the true total survives the cap"
    assert len(task["values"]) == 20
    assert task["truncated"] is True
    assert [item["value"] for item in task["values"]] == sorted(
        item["value"] for item in task["values"]
    ), "all counts are equal, so the order is alphabetical"
    assert len(inputs["values"]) <= 4 * 20, "rows are bounded by the axes, not by the members"


def test_the_report_follows_the_catalog_without_touching_the_build(
    catalog: PostgresCatalog,
) -> None:
    """A gap is a fact about the build, so a later ingest changes it and the address does not."""
    arm = f"coverage-probe-arm-{uuid.uuid4().hex[:8]}"
    later = f"coverage-probe-later-{uuid.uuid4().hex[:8]}"
    selected = [_seed(catalog, task=PICK_TASK, robot=arm) for _ in range(2)]
    build_hash = _build(catalog, selected, "coverage-probe-read-on")
    before = _report(catalog, build_hash)

    _seed(catalog, task=FOLD_TASK, robot=later)
    after = _report(catalog, build_hash)

    assert later not in _axis(before, "robot")["gaps"]
    assert later in _axis(after, "robot")["gaps"]
    assert after["catalog_size"] == before["catalog_size"] + 1
    assert after["episode_count"] == before["episode_count"] == 2
    build = catalog.get_build(build_hash)
    assert build is not None and build["name"] == "coverage-probe-read-on"
