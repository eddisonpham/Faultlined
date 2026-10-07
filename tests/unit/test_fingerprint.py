"""Behavioural fingerprints (ADR 0032).

The module's whole justification is that the measure carries information without a model, so
the tests are about *what the measure can see*: that it is blind to duration and to overall
scale, that a missing component is left out of both sides of the mean rather than scored as
zero, and that a set with planted near-duplicates collapses to the planted number. Assertions
are about relative separation (a planted pair is closer than an unrelated pair) because the
default threshold itself is calibrated by `scripts/fingerprint_calibration.py`, not by taste.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from data_engine.analysis.fingerprint import (
    COMPONENT_WEIGHTS,
    DEFAULT_THRESHOLD,
    Fingerprint,
    distance,
    fingerprint,
    redundancy_report,
)
from data_engine.analysis.quality import analyze, field_name

pytestmark = pytest.mark.unit


def _wave(
    points: int, *, period: float = 8.0, amplitude: float = 1.0, offset: float = 0.0
) -> list[float]:
    return [amplitude * math.sin(2 * math.pi * i / period) + offset for i in range(points)]


def _trace(
    values: list[float], *, step: float = 0.1, start: float = 0.0
) -> list[list[list[float]]]:
    """One run of `(seconds, score)` pairs, the shape `motion_trace` is persisted in."""
    return [[[start + index * step, value] for index, value in enumerate(values)]]


def _dims(
    *, nstd: float = 0.03, mad: float = 0.02, count: int = 6, prefix: str = "position"
) -> list[dict[str, Any]]:
    return [
        {
            "name": f"/joint_states.{prefix}[{index}]",
            "active": True,
            "discrete": False,
            "gripper": False,
            "norm_delta_std": nstd,
            "mean_abs_delta_norm": mad,
        }
        for index in range(count)
    ]


def _row(
    episode_id: str,
    *,
    trace: Any = None,
    dims: Any = None,
    stall: float = 0.0,
    gap: float = 0.0,
    verdict: str = "smooth",
    judged: int = 6,
    task: str = "",
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "task": task or f"pick {episode_id}",
        "verdict": verdict,
        "judged_dims": judged,
        "stall_ratio": stall,
        "gap_ratio": gap,
        "dims": _dims() if dims is None else dims,
        "motion_trace": _trace(_wave(40)) if trace is None else trace,
    }


def _jittered(values: list[float], *, index: int) -> list[float]:
    """A second recording of the same motion: same shape, slightly different numbers."""
    return [
        value * (1 + 0.01 * index) + 0.001 * index * math.cos(i) for i, value in enumerate(values)
    ]


# ------------------------------------------------------------------ the descriptor


def test_a_fingerprint_is_built_from_one_persisted_row_and_nothing_else() -> None:
    item = fingerprint(_row("ep-1", task="pick the cube"))
    assert item.episode_id == "ep-1"
    assert item.label == "pick the cube"
    assert item.shape and len(item.shape) > 2
    assert item.comparable


def test_a_row_with_no_task_falls_back_to_the_id_as_its_label() -> None:
    assert fingerprint({"id": "ep-9"}).label == "ep-9"


def test_the_shape_is_blind_to_how_long_the_recording_took() -> None:
    """The same motion over 4 s of samples and over 40 s of samples is the same behaviour.

    This is the property that makes a 6 s demonstration comparable to a 60 s one; without it
    every episode would only ever match another of the same duration.
    """
    quick = _trace(_wave(40), step=0.1)
    slow = _trace(_wave(40), step=1.0)

    assert distance(
        fingerprint(_row("a", trace=quick)), fingerprint(_row("b", trace=slow))
    ) == pytest.approx(0.0, abs=1e-12)


def test_the_shape_is_blind_to_the_overall_scale_of_the_motion() -> None:
    small = _trace(_wave(40))
    large = _trace([value * 7.5 for value in _wave(40)])

    assert distance(
        fingerprint(_row("a", trace=small)), fingerprint(_row("b", trace=large))
    ) == pytest.approx(0.0, abs=1e-12)


def test_a_flat_trace_has_no_shape_rather_than_a_vector_of_zeros() -> None:
    """A zero vector would sit at distance zero from every other flat episode - a false
    duplicate produced by an absence of motion rather than by a shared motion."""
    flat = fingerprint(_row("a", trace=_trace([0.0] * 40), dims=[]))
    assert flat.shape == ()
    assert not flat.comparable


def test_a_trace_with_no_clock_is_absent_and_the_episode_says_so() -> None:
    item = fingerprint(_row("a", trace=[]))
    assert item.shape == ()
    assert item.comparable  # dynamics are still there
    assert distance(item, fingerprint(_row("b", trace=[]))) is not None


def test_dynamics_are_keyed_by_the_dimensions_own_field_name() -> None:
    """An MCAP dimension is `<topic>.<path>`; the topic is where a signal came from, not what
    it is (the ADR 0018 amendment). Two recordings of the same joints under different topics
    must therefore still line up on dynamics."""
    left = fingerprint(_row("a", dims=_dims(prefix="position")))
    right = fingerprint(
        _row(
            "b",
            dims=[
                {**entry, "name": entry["name"].replace("/joint_states.", "/left/arm.")}
                for entry in _dims(prefix="position")
            ],
        )
    )

    assert left.dynamics == right.dynamics
    assert distance(left, right) == 0.0


def test_a_discrete_dimension_is_excluded_the_way_the_verdict_excludes_it() -> None:
    binary = {
        "name": "/joint_states.gripper",
        "active": True,
        "discrete": True,
        "gripper": True,
        "norm_delta_std": 0.9,
        "mean_abs_delta_norm": 0.9,
    }
    item = fingerprint(_row("a", dims=[*_dims(), binary]))
    assert all(name != "gripper" for name, _, _ in item.dynamics)


def test_the_descriptor_uses_qualitys_own_field_rule() -> None:
    """One rule, not two: a change to what a dimension's name *is* cannot make
    `analysis.quality` and `analysis.fingerprint` disagree about a gripper."""
    assert field_name("/left/gripper/joint_states.position[3]") == "position[3]"
    assert field_name("observation.state[3]") == "state[3]"


def test_the_fingerprint_matches_what_analyze_actually_produces() -> None:
    """The descriptor reads persisted columns, so it is checked against the writer."""
    quality = analyze(
        {"position[0]": _wave(30), "position[1]": _wave(30, period=5.0)},
        timestamps=[index * 0.05 for index in range(30)],
    ).to_dict()

    item = fingerprint(
        {
            "id": "ep-1",
            "task": "pick",
            "verdict": quality["verdict"],
            "judged_dims": quality["judged_dims"],
            "stall_ratio": quality["stall_ratio"],
            "gap_ratio": quality["gap_ratio"],
            "dims": quality["dims"],
            "motion_trace": quality["motion_trace"],
        }
    )
    assert item.shape
    assert [name for name, _, _ in item.dynamics] == ["position[0]", "position[1]"]


# --------------------------------------------------------------------- distance


def test_a_component_only_one_side_has_is_left_out_of_both_sides_of_the_mean() -> None:
    """Compared on what both have, renormalised by the weights present.

    Scoring a missing component as zero would push an episode with no clock away from
    everything, which is indistinguishable from "this is a different behaviour" - the exact
    confusion the module exists to avoid. Here the only real difference is the trace, and the
    distance is the shape component's own weight share of it.
    """
    has_shape = Fingerprint(
        episode_id="a",
        label="a",
        verdict="smooth",
        judged_dims=1,
        stall_ratio=0.0,
        gap_ratio=0.0,
        shape=(0.0, 2.0) + (1.0,) * 6,
        dynamics=(("position[0]", 0.0, 0.0),),
    )
    no_shape = Fingerprint(
        episode_id="b",
        label="b",
        verdict="smooth",
        judged_dims=1,
        stall_ratio=0.0,
        gap_ratio=0.0,
        shape=(),
        dynamics=(("position[0]", 0.0, 0.0),),
    )

    assert distance(has_shape, no_shape) == 0.0


def test_two_episodes_with_nothing_comparable_are_not_scored_against_each_other() -> None:
    bare = {"motion_trace": [], "dims": [], "stall_ratio": 0.1}
    assert distance(fingerprint({"id": "a", **bare}), fingerprint({"id": "b", **bare})) is None


def test_temporal_fractions_alone_are_not_enough_to_make_a_pair_comparable() -> None:
    """Two unrelated takes stall similarly all the time; that is not evidence of repetition."""
    left = Fingerprint("a", "a", "smooth", 0, 0.0, 0.0, (), ())
    right = Fingerprint("b", "b", "smooth", 0, 0.9, 0.4, (), ())
    assert distance(left, right) is None


def test_a_planted_pair_is_much_closer_than_an_unrelated_pair() -> None:
    base = _wave(60)
    planted = fingerprint(_row("planted", trace=_trace(_jittered(base, index=1))))
    original = fingerprint(_row("original", trace=_trace(base)))
    unrelated = fingerprint(_row("unrelated", trace=_trace(_wave(60, period=3.0))))

    near = distance(original, planted)
    far = distance(original, unrelated)
    assert near is not None and far is not None
    assert near < far / 3


# --------------------------------------------------------------------- the report


def test_planted_near_duplicates_collapse_to_the_number_of_distinct_behaviours() -> None:
    rows: list[dict[str, Any]] = []
    for base in range(3):
        values = _wave(60, period=8.0 + base * 5.0)
        rows.append(_row(f"base-{base}", trace=_trace(values)))
        for copy in range(1, 4):
            rows.append(_row(f"copy-{base}-{copy}", trace=_trace(_jittered(values, index=copy))))

    report = redundancy_report(rows)

    assert report.episode_count == 12
    assert report.distinct_count == 3
    assert report.redundant_count == 9
    assert report.reduction_ratio == pytest.approx(0.75)
    assert not report.truncated


def test_a_set_of_genuinely_different_behaviours_is_left_alone() -> None:
    rows = [
        _row(f"ep-{index}", trace=_trace(_wave(60, period=3.0 + index * 4.0))) for index in range(6)
    ]
    report = redundancy_report(rows)
    assert report.groups == ()
    assert report.distinct_count == 6
    assert report.reduction_ratio == 0.0


def test_the_representative_is_the_cleanest_take_not_the_first_one_seen() -> None:
    """Collapsing a group must keep the demonstration worth training on, whatever order the
    membership arrived in."""
    values = _wave(60)
    rows = [
        _row("jerky", trace=_trace(_jittered(values, index=2)), verdict="jerky"),
        _row("rough", trace=_trace(_jittered(values, index=1)), verdict="moderate", stall=0.4),
        _row("clean", trace=_trace(values), verdict="smooth", stall=0.0),
    ]

    report = redundancy_report(rows)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.representative == "clean"
    assert {item.episode_id for item in group.duplicates} == {"jerky", "rough"}


def test_a_group_says_which_signal_made_it_a_duplicate() -> None:
    """The justification for a deterministic measure is that an operator can disagree with it
    by reading it, so the reason travels with the row."""
    rows = [_row("a"), _row("b", stall=0.10)]
    report = redundancy_report(rows)

    assert len(report.groups) == 1
    duplicate = report.groups[0].duplicates[0]
    assert duplicate.reason == "temporal"
    assert duplicate.distance == pytest.approx(COMPONENT_WEIGHTS["temporal"] * 0.05, abs=1e-12)


def test_temporal_character_alone_cannot_carry_a_group_at_the_calibrated_threshold() -> None:
    """A consequence of calibrating for precision, asserted so it cannot drift silently: the
    temporal component carries the smallest weight, so a pair that differs only in how much it
    stalls has to differ by very little to collapse. Shape and dynamics are what make a
    duplicate; the temporal fractions break ties between candidates that already agree."""
    rows = [_row("a"), _row("b", stall=0.9, gap=0.2)]
    assert redundancy_report(rows).groups == ()


def test_the_report_does_not_depend_on_the_order_the_rows_arrive_in() -> None:
    values = _wave(60)
    rows = [
        _row(f"ep-{index}", trace=_trace(_jittered(values, index=index % 2))) for index in range(4)
    ]

    assert redundancy_report(rows).to_dict() == redundancy_report(list(reversed(rows))).to_dict()


def test_truncation_is_reported_rather_than_hidden() -> None:
    rows = [_row(f"ep-{index}") for index in range(5)]
    report = redundancy_report(rows, max_episodes=2)
    assert report.truncated
    assert report.episode_count == 2


def test_an_episode_with_nothing_to_compare_on_is_counted_as_distinct_and_named() -> None:
    rows = [
        _row("a"),
        _row("b"),
        {"id": "c", "task": "unscored", "verdict": "unknown", "motion_trace": [], "dims": []},
    ]
    report = redundancy_report(rows)
    assert report.incomparable == ("c",)
    assert report.compared_count == 2
    assert report.distinct_count == 2  # the kept representative plus the incomparable one


def test_an_empty_set_produces_an_empty_report_rather_than_dividing_by_zero() -> None:
    report = redundancy_report([])
    assert report.episode_count == 0
    assert report.distinct_count == 0
    assert report.reduction_ratio == 0.0


def test_an_identical_pair_collapses_at_a_zero_threshold() -> None:
    """The threshold is inclusive, so a perfect match is a duplicate at any threshold."""
    rows = [_row("a"), _row("b")]
    assert redundancy_report(rows, threshold=0.0).redundant_count == 1


def test_the_default_threshold_separates_the_planted_pair_from_the_unrelated_one() -> None:
    """The only claim the shipped default makes, asserted where it can fail."""
    base = _wave(60)
    planted = redundancy_report(
        [_row("a", trace=_trace(base)), _row("b", trace=_trace(_jittered(base, index=1)))]
    )
    unrelated = redundancy_report(
        [_row("a", trace=_trace(base)), _row("b", trace=_trace(_wave(60, period=3.0)))]
    )
    assert planted.redundant_count == 1
    assert unrelated.redundant_count == 0


# ------------------------------------------------------------------- robustness


def test_a_non_finite_signal_does_not_poison_every_distance_it_touches() -> None:
    """A NaN would make every `<= threshold` test false, which is the failure ADR 0023 exists
    to prevent one layer up: it must not become an invisible shape here either."""
    item = fingerprint(_row("a", stall=float("nan"), gap=float("inf")))
    assert item.stall_ratio == 0.0
    assert item.gap_ratio == 0.0
    assert distance(item, fingerprint(_row("b"))) is not None


def test_a_malformed_row_yields_a_thinner_fingerprint_instead_of_raising() -> None:
    for row in (
        {"id": "a", "dims": None, "motion_trace": None},
        {"id": "b", "dims": "nonsense", "motion_trace": "nonsense"},
        {"id": "c", "dims": [None, 3, {"name": None}], "motion_trace": [None, [1, 2, 3]]},
        {},
    ):
        item = fingerprint(row)
        assert isinstance(item, Fingerprint)


def test_a_trace_with_one_point_is_not_a_shape() -> None:
    assert fingerprint(_row("a", trace=[[[0.0, 1.0]]])).shape == ()


def test_a_stalled_clock_is_not_a_shape() -> None:
    """Every sample at the same instant has no time axis to resample onto."""
    assert fingerprint(_row("a", trace=[[[1.0, 1.0], [1.0, 2.0], [1.0, 3.0]]])).shape == ()


def test_a_gap_between_runs_is_still_one_chronological_series() -> None:
    split = [[[0.0, 1.0], [0.5, 2.0]], [[30.0, 1.0], [30.5, 2.0]]]
    item = fingerprint(_row("a", trace=split))
    assert len(item.shape) > 2


def test_the_pruning_shortcut_agrees_with_the_public_distance() -> None:
    """`redundancy_report`'s early exits are an optimisation, not a second definition of the
    measure: a pair collapses exactly when `distance()` says it should, for every threshold on
    the grid. Without this the fast path could quietly become the specification."""
    rows = [
        _row("a"),
        _row("b", trace=_trace(_jittered(_wave(60), index=1))),
        _row("c", trace=_trace(_wave(60, period=3.0))),
        _row("d", dims=_dims(nstd=0.4, mad=0.3)),
        _row("e", trace=[], dims=[]),
        _row("f", trace=[]),
    ]
    by_id = {str(row["id"]): row for row in rows}
    ids = list(by_id)

    for left in ids:
        for right in ids:
            if left == right:
                continue
            item = distance(fingerprint(by_id[left]), fingerprint(by_id[right]))
            for threshold in (0.0, 0.01, 0.04, 0.10, 0.25, 1.0):
                expected = item is not None and item <= threshold
                report = redundancy_report([by_id[left], by_id[right]], threshold=threshold)
                assert bool(report.groups) == expected, (left, right, threshold, item)


@pytest.mark.parametrize("threshold", [0.01, 0.04, 0.10])
def test_every_reported_distance_is_within_the_threshold(threshold: float) -> None:
    """The shortcut's soundness, stated as the property an operator relies on: nothing in the
    report is further from its representative than the threshold it was collapsed under."""
    rng_values = _wave(60)
    rows = [
        _row(f"ep-{index}", trace=_trace(_jittered(rng_values, index=index % 3)))
        for index in range(12)
    ]
    report = redundancy_report(rows, threshold=threshold)
    for group in report.groups:
        assert group.duplicates
        for duplicate in group.duplicates:
            assert duplicate.distance <= threshold
            assert duplicate.reason in {"shape", "dynamics", "temporal"}


def test_the_report_serialises_everything_a_caller_needs_to_judge_it() -> None:
    body = redundancy_report([_row("a"), _row("b", stall=0.9)]).to_dict()
    assert body["method"] == "behavioural-fingerprint-v1"
    assert body["components"] == COMPONENT_WEIGHTS
    assert body["threshold"] == DEFAULT_THRESHOLD
    assert set(body) >= {
        "episode_count",
        "compared_count",
        "distinct_count",
        "redundant_count",
        "reduction_ratio",
        "incomparable",
        "groups",
        "truncated",
    }
