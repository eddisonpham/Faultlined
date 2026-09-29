"""Unit tests for episode motion-quality signals and summary assembly."""

from __future__ import annotations

import math

import pytest

from data_engine.analysis.quality import (
    DimQuality,
    _verdict,
    analyze,
)
from data_engine.catalog.repository import _assemble_quality_summary, _zscore


def _ramp(count: int, step: float = 1.0) -> list[float]:
    return [i * step for i in range(count)]


@pytest.mark.unit
def test_linear_ramp_scores_movement_and_normalized_jerk() -> None:
    quality = analyze({"action[0]": _ramp(10)})
    # Delta is 1.0 per transition: mean L2 movement is exactly 1.0.
    assert quality.movement_score == pytest.approx(1.0)
    # Jerk normalizes by the dim's range (9.0), so 10-unit ramps score the same.
    assert quality.jerk_score == pytest.approx(1.0 / 9.0)
    assert quality.stall_ratio == 0.0
    assert quality.frame_count == 10
    assert quality.verdict == "smooth"  # constant delta -> zero delta-sigma


@pytest.mark.unit
def test_jerk_is_scale_free_across_dims() -> None:
    quality = analyze({"small": _ramp(10), "big": _ramp(10, step=100.0)})
    assert quality.jerk_score == pytest.approx(1.0 / 9.0)
    assert {dim.name for dim in quality.dims} == {"small", "big"}
    assert all(dim.active for dim in quality.dims)
    assert all(not dim.discrete for dim in quality.dims)


@pytest.mark.unit
def test_stall_ratio_counts_flat_transitions() -> None:
    quality = analyze({"action[0]": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]})
    # 4 of 5 transitions move nothing at all.
    assert quality.stall_ratio == pytest.approx(0.8)
    assert quality.movement_score == pytest.approx(0.2)
    assert quality.jerk_score == pytest.approx(0.2)
    # Two unique values reads as a discrete dim, which is excluded from the verdict.
    assert quality.dims[0].discrete is True
    assert quality.verdict == "unknown"


@pytest.mark.unit
def test_degenerate_inputs_score_zero_and_unknown() -> None:
    for series in ({}, {"a": [1.0]}, {"a": [], "b": []}):
        quality = analyze(series)
        assert quality.verdict == "unknown"
        assert quality.movement_score == 0.0
        assert quality.jerk_score == 0.0
        assert quality.stall_ratio == 0.0


@pytest.mark.unit
def test_mismatched_series_lengths_are_rejected() -> None:
    with pytest.raises(ValueError):
        analyze({"a": [0.0, 1.0], "b": [0.0]})


def _dim(
    name: str, norm_std: float, *, gripper: bool = False, discrete: bool = False
) -> DimQuality:
    return DimQuality(
        name=name,
        active=True,
        discrete=discrete,
        gripper=gripper,
        norm_delta_std=norm_std,
        mean_abs_delta_norm=norm_std,
    )


@pytest.mark.unit
def test_verdict_uses_absolute_normalized_sigma_bands() -> None:
    """Absolute bands (ADR 0018); the visualizer's relative bands are degenerate."""
    smooth = [_dim("a", 0.005), _dim("b", 0.01)]
    assert _verdict(smooth) == "smooth"

    # One moderate dim lifts the episode to moderate; one jerky dim to jerky.
    assert _verdict([*smooth, _dim("c", 0.05)]) == "moderate"
    assert _verdict([*smooth, _dim("d", 0.2)]) == "jerky"

    # A gripper's bang-bang open/close never drags the episode's verdict.
    assert _verdict([*smooth, _dim("gripper", 0.5, gripper=True)]) == "smooth"

    # Discrete dims are not judged at all.
    assert _verdict([_dim("mode", 9.0, discrete=True)]) == "unknown"

    assert _verdict([]) == "unknown"
    assert _verdict([_dim("flat", 0.0)]) == "smooth"


@pytest.mark.unit
def test_verdict_ignores_inactive_dims() -> None:
    inactive = DimQuality(
        name="frozen",
        active=False,
        discrete=False,
        gripper=False,
        norm_delta_std=0.0,
        mean_abs_delta_norm=0.0,
    )
    assert _verdict([inactive]) == "unknown"


@pytest.mark.unit
def test_to_dict_is_json_ready() -> None:
    data = analyze({"action[0]": _ramp(10), "gripper": [0.0, 1.0] * 5}).to_dict()
    assert set(data) == {
        "frame_count",
        "movement_score",
        "jerk_score",
        "stall_ratio",
        "verdict",
        "dims",
    }
    assert all(set(dim) >= {"name", "active", "discrete", "gripper"} for dim in data["dims"])


# ------------------------------------------------------- summary assembly


def _row(
    episode_id: str,
    frames: int,
    *,
    movement: float = 0.0,
    jerk: float = 0.0,
    stall: float = 0.0,
    verdict: str = "smooth",
    dims: list[dict] | None = None,
) -> dict:
    return {
        "episode_id": episode_id,
        "episode_key": f"episode_index={episode_id}",
        "frame_count": frames,
        "movement_score": movement,
        "jerk_score": jerk,
        "stall_ratio": stall,
        "verdict": verdict,
        "dims": dims or [],
    }


@pytest.mark.unit
def test_summary_of_no_episodes_is_empty_not_broken() -> None:
    summary = _assemble_quality_summary([])
    assert summary["episode_count"] == 0
    assert summary["speed_distribution"] == []
    assert summary["length"]["histogram"] == []
    assert summary["outliers"]["jerk"] == []


@pytest.mark.unit
def test_summary_groups_verdicts_and_ranks_outliers() -> None:
    rows = [
        _row("e1", 10, movement=0.5, jerk=0.9, stall=0.1, verdict="jerky"),
        _row("e2", 20, movement=1.5, jerk=0.1, stall=0.0),
        _row("e3", 120, movement=2.5, jerk=0.2, stall=0.4, verdict="moderate"),
    ]
    summary = _assemble_quality_summary(rows)

    assert summary["episode_count"] == 3
    assert summary["verdicts"] == {"jerky": 1, "smooth": 1, "moderate": 1}
    assert [item["episode_id"] for item in summary["speed_distribution"]] == ["e1", "e2", "e3"]
    assert [item["episode_id"] for item in summary["outliers"]["jerk"]] == ["e1", "e3", "e2"]
    assert [item["episode_id"] for item in summary["outliers"]["stall"]] == ["e3", "e1", "e2"]
    # e3 is the length outlier (120 vs ~50 mean).
    assert summary["outliers"]["length"][0]["episode_id"] == "e3"
    assert summary["outliers"]["length"][0]["value"] > 1

    length = summary["length"]
    assert length["count"] == 3
    assert length["min"] == 10
    assert length["max"] == 120
    assert sum(bin["count"] for bin in length["histogram"]) == 3


@pytest.mark.unit
def test_heat_matrix_aligns_dims_and_marks_missing() -> None:
    rows = [
        _row("e1", 10, dims=[{"name": "a", "norm_delta_std": 0.1}]),
        _row("e2", 10, dims=[{"name": "b", "norm_delta_std": 0.2}]),
    ]
    matrix = _assemble_quality_summary(rows)["heat_matrix"]
    assert matrix["dims"] == ["a", "b"]
    assert matrix["episodes"][0]["values"] == [pytest.approx(0.1), None]
    assert matrix["episodes"][1]["values"] == [None, pytest.approx(0.2)]


@pytest.mark.unit
def test_zscore_handles_degenerate_populations() -> None:
    assert _zscore(5, {"mean": 5.0, "std": 0.0}) == 0.0
    assert _zscore(5, {"mean": 5.0, "std": None}) == 0.0
    assert _zscore(5, None) == 0.0
    assert _zscore(7, {"mean": 5.0, "std": 2.0}) == pytest.approx(1.0)


@pytest.mark.unit
def test_length_histogram_bins_do_not_overflow() -> None:
    summary = _assemble_quality_summary([_row("e1", 10), _row("e2", 10)])
    # Identical lengths: width falls back to 1 and everything lands in one bin.
    assert sum(bin["count"] for bin in summary["length"]["histogram"]) == 2
    assert summary["length"]["std"] == 0.0 or math.isclose(summary["length"]["std"], 0.0)
