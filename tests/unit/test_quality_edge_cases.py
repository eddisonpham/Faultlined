"""Edge cases the quality metrics had to be broken to find."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from data_engine.analysis.quality import GAP_INTERVAL_FACTOR, analyze

pytestmark = [pytest.mark.unit]


def _ramp(count: int, step: float = 1.0) -> list[float]:
    return [i * step for i in range(count)]


def _sine(count: int, period: float = 20.0, dims: int = 3) -> dict[str, list[float]]:
    return {f"joint_{i}": [math.sin(j / period + i) for j in range(count)] for i in range(dims)}


@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("nan"), math.inf, -math.inf])
def test_a_non_finite_value_stops_the_analysis_instead_of_poisoning_it(bad: float) -> None:
    """One NaN in one encoder used to write `movement_score = NaN` to the catalog."""
    quality = analyze({"a": [1.0, bad, 3.0, 4.0], "b": _ramp(4)})

    assert quality.nonfinite == 1
    assert quality.verdict == "unknown"
    assert quality.movement_score == 0.0
    assert quality.jerk_score == 0.0
    for value in (quality.movement_score, quality.jerk_score, quality.stall_ratio):
        assert math.isfinite(value), "a non-finite value must never reach the catalog"


@pytest.mark.unit
def test_an_entirely_non_finite_dimension_is_counted_not_ignored() -> None:
    quality = analyze({"a": [float("nan")] * 8, "b": [float("nan")] * 8})
    assert quality.nonfinite == 16
    assert quality.verdict == "unknown"
    assert quality.dims == ()


@pytest.mark.unit
def test_a_finite_episode_still_scores_normally() -> None:
    """The guard must not be a blanket refusal to measure."""
    quality = analyze(_sine(200))
    assert quality.nonfinite == 0
    assert quality.verdict in {"smooth", "moderate", "jerky"}
    assert quality.movement_score > 0


@pytest.mark.unit
def test_extreme_magnitudes_do_not_crash_the_ingest() -> None:
    """`sqrt(sum(d*d))` raised OverflowError above ~1e154 and failed a whole job."""
    quality = analyze({"a": [0.0, 1e200, 0.0, 1e200] * 20, "b": [0.0, 1e200] * 40})
    assert math.isfinite(quality.movement_score)
    assert quality.movement_score == pytest.approx(1e200 * math.sqrt(2.0), rel=1e-6)


@pytest.mark.unit
def test_values_below_the_double_denormal_range_do_not_divide_by_zero() -> None:
    quality = analyze({"a": [0.0, 1e-300, 0.0, 1e-300] * 20})
    assert math.isfinite(quality.movement_score)
    assert math.isfinite(quality.jerk_score)


@pytest.mark.unit
def test_ragged_series_are_refused_rather_than_scored() -> None:
    with pytest.raises(ValueError, match="equal length"):
        analyze({"a": [1.0, 2.0], "b": [1.0]})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("series", "label"),
    [
        ({}, "no dimensions"),
        ({"a": []}, "no frames"),
        ({"a": [1.0]}, "one frame"),
        ({"a": [1.0] * 50, "b": [2.0] * 50}, "constant"),
    ],
)
def test_degenerate_episodes_score_zero_and_say_unknown(
    series: dict[str, list[float]], label: str
) -> None:
    quality = analyze(series)
    assert quality.verdict == "unknown", label
    assert quality.movement_score == 0.0, label
    assert quality.jerk_score == 0.0, label
    assert quality.judged_dims == 0, label


@pytest.mark.unit
@pytest.mark.parametrize(
    ("series", "label"),
    [
        ({"a": [0, 1] * 40}, "discrete"),
        ({"gripper": [0.0, 1.0] * 40}, "gripper only"),
    ],
)
def test_unjudgeable_dimensions_do_not_veto_but_still_get_measured(
    series: dict[str, list[float]], label: str
) -> None:
    """A dim excluded from the verdict is not a dim excluded from measurement."""
    quality = analyze(series)
    assert quality.verdict == "unknown", label
    assert quality.judged_dims == 0, label
    assert quality.movement_score == pytest.approx(1.0), label


@pytest.mark.unit
def test_a_long_episode_is_still_measured() -> None:
    """The bound is on memory, not on capability."""
    quality = analyze(_sine(200_000))
    assert quality.frame_count == 200_000
    assert quality.verdict in {"smooth", "moderate", "jerky"}


@pytest.mark.unit
def test_a_continuously_recorded_episode_reports_ok() -> None:
    quality = analyze(_sine(200), timestamps=[j / 30.0 for j in range(200)])
    assert quality.integrity == "ok"
    assert quality.max_gap_seconds == pytest.approx(1 / 30.0, rel=1e-6)
    assert quality.gap_ratio == 0.0


@pytest.mark.unit
def test_a_dropped_recording_is_visible_which_no_per_frame_statistic_could_see() -> None:
    """The point of the temporal half of the signal."""
    smooth = _sine(200)
    continuous = analyze(smooth, timestamps=[j / 30.0 for j in range(200)])
    stalled = analyze(smooth, timestamps=[j / 30.0 for j in range(200)])
    assert continuous.movement_score == stalled.movement_score

    frozen = [j / 30.0 for j in range(51)]
    frozen += [frozen[50] + 30.0 + (j - 51) / 30.0 for j in range(51, 200)]
    gapped = analyze(smooth, timestamps=frozen)

    assert gapped.integrity == "gapped"
    assert gapped.max_gap_seconds == pytest.approx(30.0, rel=1e-6)
    assert gapped.gap_ratio == pytest.approx(1 / 199, rel=1e-6)
    assert gapped.movement_score == continuous.movement_score


@pytest.mark.unit
def test_the_gap_threshold_is_relative_so_healthy_sampling_rates_both_pass() -> None:
    """A fixed millisecond bound would call a 1 kHz torque loop broken."""
    for rate in (10.0, 30.0, 1000.0):
        quality = analyze(_sine(200), timestamps=[j / rate for j in range(200)])
        assert quality.integrity == "ok", rate


@pytest.mark.unit
def test_gap_detection_uses_the_median_so_one_gap_cannot_hide_itself() -> None:
    clock = [j / 30.0 for j in range(101)]
    clock += [clock[100] + 1.0 + (j - 101) / 30.0 for j in range(101, 200)]
    quality = analyze(_sine(200), timestamps=clock)
    assert quality.integrity == "gapped"
    assert quality.gap_ratio == pytest.approx(1 / 199, rel=1e-6)
    assert quality.max_gap_seconds > GAP_INTERVAL_FACTOR / 30.0


@pytest.mark.unit
def test_a_clock_that_went_backwards_is_not_reported_as_a_clean_recording() -> None:
    """Every rate derived from a decreasing clock is a lie, not a healthy episode."""
    clock = [j / 30.0 for j in range(200)]
    clock[100] = 0.0
    quality = analyze(_sine(200), timestamps=clock)
    assert quality.integrity == "gapped"
    assert quality.max_gap_seconds is None


@pytest.mark.unit
def test_no_clock_means_integrity_is_unknown_not_ok() -> None:
    """`ok` is a claim about time and cannot be made without a clock."""
    assert analyze(_sine(200)).integrity == "unknown"
    assert analyze(_sine(200)).max_gap_seconds is None


@pytest.mark.unit
def test_timestamps_must_cover_the_frames_they_describe() -> None:
    with pytest.raises(ValueError, match="same frames"):
        analyze(_sine(200), timestamps=[0.0, 1.0])


@pytest.mark.unit
def test_a_non_finite_clock_degrades_to_unknown_rather_than_raising() -> None:
    clock = [j / 30.0 for j in range(200)]
    clock[10] = float("nan")
    quality = analyze(_sine(200), timestamps=clock)
    assert quality.integrity == "unknown"
    assert quality.verdict in {"smooth", "moderate", "jerky"}


@pytest.mark.unit
def test_a_ragged_synthetic_episode_is_rejected_at_the_contract() -> None:
    """It used to validate, then die with an IndexError inside the series builder."""
    from data_engine.api.schemas import SyntheticEpisode

    with pytest.raises(ValidationError, match="same width"):
        SyntheticEpisode(
            task="t",
            robot="r",
            timestamps=[0.0, 1.0, 2.0],
            observations=[[1.0, 2.0], [3.0], [4.0, 5.0]],
            actions=[[0.0], [0.0], [0.0]],
        )

    with pytest.raises(ValidationError, match="same width"):
        SyntheticEpisode(
            task="t",
            robot="r",
            timestamps=[0.0, 1.0],
            observations=[[1.0, 2.0], [3.0, 4.0]],
            actions=[[0.0], [0.0, 1.0]],
        )


@pytest.mark.unit
def test_a_clock_wider_than_the_retained_window_still_reports_its_whole_log_maximum() -> None:
    """The streaming reader keeps a bounded window; the log it came from is not bounded."""
    window = [j / 50.0 for j in range(200)]
    quality = analyze(
        _sine(200),
        timestamps=window,
        max_interval_seconds=7200.0,
    )
    assert quality.max_gap_seconds == 7200.0
    assert quality.integrity == "gapped"
    assert quality.gap_ratio == 0.0, "a window's ratio is not the log's ratio"


@pytest.mark.unit
def test_a_whole_log_maximum_within_the_threshold_leaves_the_verdict_alone() -> None:
    """A slightly larger interval than the window saw is still a healthy stream."""
    quality = analyze(
        _sine(200),
        timestamps=[j / 50.0 for j in range(200)],
        max_interval_seconds=0.021,
    )
    assert quality.integrity == "ok"
    assert quality.max_gap_seconds == pytest.approx(0.021, rel=1e-6)


@pytest.mark.unit
def test_a_non_finite_whole_log_maximum_is_refused_rather_than_believed() -> None:
    quality = analyze(
        _sine(200),
        timestamps=[j / 50.0 for j in range(200)],
        max_interval_seconds=float("inf"),
    )
    assert quality.integrity == "ok"
    assert math.isfinite(quality.max_gap_seconds)
