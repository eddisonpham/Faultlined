"""Coverage as a pure fold over a catalog query result (ADR 0033)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from data_engine.analysis.coverage import (
    AXES,
    GAP_LIMIT,
    VALUE_LIMIT,
    CoverageReport,
    coverage_report,
)

pytestmark = pytest.mark.unit


def _row(
    axis: str,
    value: str,
    *,
    build_count: int,
    catalog_count: int,
    build_distinct: int = 1,
    catalog_distinct: int = 1,
    gap_total: int = 0,
    build_rank: int = 1,
    value_rank: int = 1,
    build_total: int = 2,
    catalog_total: int = 5,
    **overrides: Any,
) -> dict[str, Any]:
    """One `build_coverage_inputs` row, in the shape psycopg hands back."""
    return {
        "axis": axis,
        "value": value,
        "build_count": build_count,
        "catalog_count": catalog_count,
        "build_distinct": build_distinct,
        "catalog_distinct": catalog_distinct,
        "gap_total": gap_total,
        "build_total": build_total,
        "catalog_total": catalog_total,
        "build_rank": build_rank,
        "value_rank": value_rank,
        **overrides,
    }


def _inputs(**overrides: Any) -> dict[str, Any]:
    """A two-of-five build over a catalog that holds another robot and two absent tasks."""
    inputs: dict[str, Any] = {
        "episode_count": 2,
        "catalog_size": 5,
        "values": [
            _row("format", "synthetic-json", build_count=2, catalog_count=5),
            _row(
                "robot",
                "so101",
                build_count=2,
                catalog_count=3,
                catalog_distinct=2,
                gap_total=1,
            ),
            _row(
                "robot",
                "ur5e",
                build_count=0,
                catalog_count=2,
                catalog_distinct=2,
                gap_total=1,
                build_rank=2,
                value_rank=2,
            ),
            _row(
                "task",
                "pick",
                build_count=2,
                catalog_count=2,
                catalog_distinct=3,
                gap_total=2,
            ),
            _row(
                "verdict",
                "smooth",
                build_count=1,
                catalog_count=4,
                build_distinct=2,
                catalog_distinct=2,
            ),
            _row(
                "verdict",
                "unknown",
                build_count=1,
                catalog_count=1,
                build_distinct=2,
                catalog_distinct=2,
                build_rank=2,
            ),
        ],
        "vocabulary": {
            "total": 3,
            "missing_total": 2,
            "gaps": [
                {"id": "voc_1", "label": "fold", "catalog_episodes": 2},
                {"id": "voc_2", "label": "place the can", "catalog_episodes": 0},
            ],
        },
    }
    inputs.update(overrides)
    return inputs


def _report(**overrides: Any) -> CoverageReport:
    return coverage_report("bld_" + "a" * 64, _inputs(**overrides))


@pytest.mark.unit
def test_the_report_counts_the_build_against_the_catalog_it_was_drawn_from() -> None:
    report = _report()

    assert report.build_hash == "bld_" + "a" * 64
    assert report.episode_count == 2
    assert report.catalog_size == 5
    assert report.coverage_ratio == pytest.approx(0.4)
    assert report.to_dict()["method"] == "build-coverage-v1"


@pytest.mark.unit
def test_every_axis_is_reported_once_in_a_fixed_order_with_its_label() -> None:
    """The axes are the catalog's columns, so the surface and the schema cannot drift apart."""
    report = _report()

    assert [axis.name for axis in report.axes] == [name for name, _ in AXES]
    assert [axis.label for axis in report.axes] == ["Task", "Embodiment", "Format", "Verdict"]
    assert report.axis("robot") is not None
    assert report.axis("robot").label == "Embodiment"  # type: ignore[union-attr]
    assert report.axis("no-such-axis") is None


@pytest.mark.unit
def test_the_distribution_carries_the_share_of_the_build_each_value_accounts_for() -> None:
    smooth = _report().axis("verdict")
    assert smooth is not None
    assert [(item.value, item.count, item.share) for item in smooth.values] == [
        ("smooth", 1, 0.5),
        ("unknown", 1, 0.5),
    ]


@pytest.mark.unit
def test_values_are_ordered_by_count_then_value_whatever_order_the_rows_arrive_in() -> None:
    """Two runs over one catalog have to produce the same bytes, so the order is the report's."""
    forwards = _report()
    backwards = coverage_report(
        "bld_" + "a" * 64, _inputs(values=list(reversed(_inputs()["values"])))
    )

    assert forwards.to_dict() == backwards.to_dict()
    robot = forwards.axis("robot")
    assert robot is not None
    assert [item.value for item in robot.values] == ["so101"]


@pytest.mark.unit
def test_a_catalog_value_the_build_left_out_is_a_gap() -> None:
    robot = _report().axis("robot")
    assert robot is not None

    assert robot.present == 1
    assert robot.gaps == ("ur5e",)
    assert robot.missing == 1
    assert robot.truncated is False


@pytest.mark.unit
def test_gaps_are_alphabetical_rather_than_in_query_order() -> None:
    rows = _inputs()["values"] + [
        _row(
            "robot",
            "abb-irb",
            build_count=0,
            catalog_count=1,
            catalog_distinct=3,
            gap_total=2,
            build_rank=3,
            value_rank=3,
        )
    ]
    robot = coverage_report("bld_" + "a" * 64, _inputs(values=rows)).axis("robot")

    assert robot is not None
    assert robot.gaps == ("abb-irb", "ur5e")


@pytest.mark.unit
def test_task_gaps_come_from_the_vocabulary_not_from_the_stored_episodes() -> None:
    """`place the can` has no episode anywhere, and it is still a gap: a gap has to be nameable."""
    task = _report().axis("task")
    assert task is not None

    assert [item.value for item in task.values] == ["pick"]
    assert task.gaps == ("fold", "place the can")
    assert task.missing == 2
    assert _report().vocabulary_total == 3
    assert _report().vocabulary_missing == 2


@pytest.mark.unit
def test_the_task_axis_keeps_its_gap_total_when_the_gap_list_is_capped() -> None:
    vocabulary = {
        "total": 40,
        "missing_total": 40,
        "gaps": [{"id": f"voc_{index}", "label": f"task {index:02d}"} for index in range(40)],
    }
    report = _report(vocabulary=vocabulary)
    task = report.axis("task")

    assert task is not None
    assert len(task.gaps) == GAP_LIMIT
    assert list(task.gaps) == sorted(task.gaps), "a capped list is the alphabetical head"
    assert task.missing == 40
    assert task.truncated is True


@pytest.mark.unit
def test_a_gap_list_cut_by_the_query_still_reports_how_many_gaps_there_were() -> None:
    """The query caps rows, so counting the rows it returned would report "no gaps" for a cut
    axis."""
    rows = _inputs()["values"] + [
        _row(
            "verdict",
            "(unscored)",
            build_count=0,
            catalog_count=3,
            build_distinct=2,
            catalog_distinct=3,
            gap_total=1,
            value_rank=3,
        )
    ]
    rows = [{**row, "gap_total": 1} if row["axis"] == "verdict" else row for row in rows]
    capped = [row for row in rows if row["axis"] != "verdict" or row["build_count"] > 0]
    verdict = coverage_report("bld_" + "a" * 64, _inputs(values=capped)).axis("verdict")

    assert verdict is not None
    assert verdict.gaps == ()
    assert verdict.missing == 1
    assert verdict.truncated is True


@pytest.mark.unit
def test_a_capped_distribution_says_so_and_keeps_the_true_total() -> None:
    rows = [
        _row(
            "task",
            f"task {index:02d}",
            build_count=1,
            catalog_count=1,
            build_distinct=25,
            catalog_distinct=25,
            build_rank=index + 1,
            value_rank=index + 1,
        )
        for index in range(25)
    ]
    task = coverage_report("bld_" + "a" * 64, _inputs(values=rows)).axis("task")

    assert task is not None
    assert task.present == 25
    assert len(task.values) == VALUE_LIMIT
    assert task.truncated is True


@pytest.mark.unit
def test_the_limits_are_the_ones_the_adr_fixed_and_a_caller_can_lower_them() -> None:
    assert (VALUE_LIMIT, GAP_LIMIT) == (20, 20)

    rows = [
        _row("task", f"task {index}", build_count=1, catalog_count=1, build_distinct=3)
        for index in range(3)
    ]
    task = coverage_report("bld_" + "a" * 64, _inputs(values=rows), value_limit=1).axis("task")

    assert task is not None
    assert len(task.values) == 1
    assert task.truncated is True


@pytest.mark.unit
def test_an_empty_build_is_reported_as_empty_rather_than_as_missing() -> None:
    report = coverage_report(
        "bld_" + "a" * 64,
        {
            "episode_count": 0,
            "catalog_size": 5,
            "values": [],
            "vocabulary": {"total": 0, "missing_total": 0, "gaps": []},
        },
    )

    assert report.coverage_ratio == 0.0
    assert [axis.values for axis in report.axes] == [(), (), (), ()]
    assert [axis.gaps for axis in report.axes] == [(), (), (), ()]
    assert report.vocabulary_missing == 0


@pytest.mark.unit
def test_a_catalog_of_nothing_does_not_divide_by_it() -> None:
    report = coverage_report("bld_" + "a" * 64, {"episode_count": 0, "catalog_size": 0})

    assert report.coverage_ratio == 0.0
    assert report.episode_count == 0


@pytest.mark.unit
def test_malformed_input_reads_as_an_empty_report_rather_than_raising() -> None:
    """A dark coverage panel for a reason nobody can see is worse than one that says nothing."""
    report = coverage_report(
        "bld_" + "a" * 64,
        {
            "episode_count": "two",
            "catalog_size": None,
            "values": [
                "not a row",
                {"axis": "robot", "value": None, "build_count": 2},
                {"axis": "verdict", "value": "smooth", "build_count": True},
            ],
            "vocabulary": "not a mapping",
        },
    )

    assert report.episode_count == 0
    assert report.catalog_size == 0
    robot = report.axis("robot")
    verdict = report.axis("verdict")
    assert robot is not None and verdict is not None
    assert robot.values[0].value == ""
    assert robot.values[0].count == 2
    assert verdict.values == (), "a bool is not a count"
    assert report.vocabulary_total == 0
    assert report.to_dict()["axes"][0]["values"] == []


@pytest.mark.unit
def test_non_finite_and_non_numeric_counts_are_read_as_zero() -> None:
    report = coverage_report(
        "bld_" + "a" * 64,
        {
            "episode_count": Decimal("NaN"),
            "catalog_size": float("inf"),
            "values": [
                _row("robot", "so101", build_count=float("-inf"), catalog_count=0),
                _row("robot", "ur5e", build_count=2, catalog_count=2),
            ],
        },
    )

    assert report.episode_count == 0
    assert report.catalog_size == 0
    robot = report.axis("robot")
    assert robot is not None
    assert [item.value for item in robot.values] == ["ur5e"]
    assert robot.gaps == (), "a row that read as zero does not make a gap out of nothing"
    assert robot.values[0].share == 0.0


@pytest.mark.unit
def test_a_numeric_total_from_postgres_is_read_as_a_count() -> None:
    """`sum()` over a `bigint` is `numeric`: the first run of this reported every total as 0."""
    report = coverage_report(
        "bld_" + "a" * 64,
        {
            "episode_count": Decimal("2"),
            "catalog_size": Decimal("5"),
            "values": [
                _row("robot", "so101", build_count=Decimal("2"), catalog_count=Decimal("3"))
            ],
            "vocabulary": {"total": Decimal("3"), "missing_total": Decimal("1"), "gaps": []},
        },
    )

    assert report.episode_count == 2
    assert report.catalog_size == 5
    assert report.coverage_ratio == pytest.approx(0.4)
    assert report.vocabulary_total == 3
    assert report.vocabulary_missing == 1
    robot = report.axis("robot")
    assert robot is not None
    assert robot.values[0].count == 2
    assert robot.values[0].share == 1.0


@pytest.mark.unit
def test_rows_for_an_axis_nobody_asked_about_are_ignored() -> None:
    rows = _inputs()["values"] + [_row("not-an-axis", "whatever", build_count=9, catalog_count=9)]
    report = coverage_report("bld_" + "a" * 64, _inputs(values=rows))

    assert [axis.name for axis in report.axes] == [name for name, _ in AXES]
    assert report.episode_count == 2
