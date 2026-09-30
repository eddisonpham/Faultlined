"""Episode motion-quality analysis (ADR 0018).

Pure functions over frame series, computed once at ingest while the rows are
already in memory and persisted as a small summary alongside the episode. The
formulas follow the closest production system, huggingface/lerobot-dataset-visualizer
(source-log #46 in `agents/research/source-log.md`):

- **movement score** — mean L2 norm of frame-to-frame deltas across dims (per frame
  transition; multiply by fps for units/s).
- **jerk score** — mean absolute frame-to-frame delta normalized by the dim's range
  (max - min), averaged over *active* dims only.
- **active dim** — p95(|delta|) >= 0.1% of the dim's range; **discrete dim** — at
  most 4 unique values (binary grippers etc.). Discrete dims are still reported but
  excluded from the verdict.
- **stall ratio** — fraction of frame transitions where every dim moved less than
  0.1% of its range (a "motion stall" in the visualizer's filtering panel).
- **verdict** — `smooth` / `moderate` / `jerky` from absolute bands on normalized
  delta-sigma (<0.02 smooth, <0.1 moderate, >=0.1 jerky) over *judged* dims (active,
  non-discrete, non-gripper: a binary gripper is jerky by nature). The visualizer
  bands dims relative to the roughest dim, which is degenerate for an episode whose
  dims move alike — the max dim always lands in its "jerky" bucket — so the verdict
  uses absolute bands instead (ADR 0018).

Degenerate inputs (fewer than 2 frames, no numeric dims) score 0 and verdict
`unknown` rather than raising: a quality signal must never fail an ingest.

Two properties of the original formulation did not survive contact with real data
and are stated here because both change what a verdict means.

**Non-finite values are not a verdict, they are an absence of one.** The first
version computed over whatever it was given, so a single NaN from one encoder put
`movement_score = NaN` in the catalog. The verdict happened to come out `unknown`
- not by design but because every comparison against NaN is false - while the
*scores* were written to the database, where `_mean` over a population containing
one NaN returns NaN for the whole population. One corrupt sensor silently moved
every dataset-level statistic in the product. A non-finite value now stops the
analysis, reports how many were seen, and scores zero.

**The metric could not see time.** `analyze` received values only, so an episode
where the robot froze for five minutes and one where it moved continuously scored
identically - `movement_score` is an L2 norm *per frame transition*, and a frozen
robot's frames are the same however long the freeze was. Everything the catalog
already computed about timestamp gaps lived in a different subsystem
(`monitoring.features.max_episode_timestamp_gap_seconds`) and never reached the
episode's own quality record. `analyze` now takes optional timestamps and reports
`max_gap_seconds`, `gap_ratio` and an `integrity` verdict beside the motion
verdict. This is the one signal in the product that can distinguish "the robot was
still" from "the recorder stopped", and no per-frame statistic ever could.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

#: p95(|delta|) below this fraction of the dim's range means the dim is inactive.
ACTIVITY_THRESHOLD = 0.001
#: A dim with at most this many unique values is discrete (gripper, button, mode).
DISCRETE_MAX_UNIQUE = 4
#: Verdict bands on normalized delta-sigma (sigma of frame deltas / dim range).
_GRIPPER = re.compile("grip", re.IGNORECASE)

MIN_FRAMES = 2

#: A transition whose interval exceeds this multiple of the median interval is a
#: gap: the recorder stopped, the clock jumped, or a message was lost. Median
#: rather than mean, because one enormous gap would otherwise raise the bar above
#: every legitimate interval and hide itself.
GAP_INTERVAL_FACTOR = 5.0

#: Verdict bands on the *median* judged dimension's normalized delta-sigma.
#:
#: The original bands were the worst dimension, on the reasoning that a robot with
#: one bad encoder is a robot with a bad encoder. Measured against real logs, that
#: made a single miscalibrated channel out of eighteen decide the fate of the whole
#: episode, and the operator had no way to see which dimension did it without
#: opening the JSON. The verdict is now the median - a majority of the arm has to
#: be rough - and the worst dimension is still reported, as `worst_dim` and
#: `worst_verdict`, so nothing is hidden by the change. See ADR 0023.
SMOOTH_SIGMA = 0.02
JERKY_SIGMA = 0.1


@dataclass(frozen=True, slots=True)
class DimQuality:
    """Per-dimension motion character of one episode."""

    name: str
    active: bool
    discrete: bool
    gripper: bool
    norm_delta_std: float
    """std(frame-to-frame delta) / range: scale-free jerkiness of this dim."""

    mean_abs_delta_norm: float
    """mean(|delta|) / range over the episode."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "active": self.active,
            "discrete": self.discrete,
            "gripper": self.gripper,
            "norm_delta_std": self.norm_delta_std,
            "mean_abs_delta_norm": self.mean_abs_delta_norm,
        }


@dataclass(frozen=True, slots=True)
class EpisodeQuality:
    """Motion-quality summary of one episode; the catalog keeps this, not the frames."""

    frame_count: int
    movement_score: float
    jerk_score: float
    stall_ratio: float
    verdict: str
    dims: tuple[DimQuality, ...]
    nonfinite: int = 0
    """How many values were NaN or infinite. Non-zero means nothing below is a measurement."""

    max_gap_seconds: float | None = None
    """Longest interval between consecutive samples. `None` when no clock was supplied."""

    gap_ratio: float = 0.0
    """Fraction of transitions whose interval exceeded 5x the median interval."""

    integrity: str = "unknown"
    """`ok` / `gapped` / `unknown` - whether the recording was continuous in time."""

    worst_verdict: str = "unknown"
    """The worst single-dimension band, kept so the median verdict hides nothing."""

    worst_dim: str | None = None
    """Which dimension produced `worst_verdict`, so the UI can name it."""

    judged_dims: int = 0
    """How many dimensions actually judged motion (active, non-discrete, non-gripper)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_count": self.frame_count,
            "movement_score": self.movement_score,
            "jerk_score": self.jerk_score,
            "stall_ratio": self.stall_ratio,
            "verdict": self.verdict,
            "nonfinite": self.nonfinite,
            "max_gap_seconds": self.max_gap_seconds,
            "gap_ratio": self.gap_ratio,
            "integrity": self.integrity,
            "worst_verdict": self.worst_verdict,
            "worst_dim": self.worst_dim,
            "judged_dims": self.judged_dims,
            "dims": [dim.to_dict() for dim in self.dims],
        }


def analyze(
    series: Mapping[str, Sequence[float]],
    *,
    timestamps: Sequence[float] | None = None,
) -> EpisodeQuality:
    """Compute motion-quality signals from named per-dimension frame series.

    `timestamps`, when given, must cover the same frames and enables the temporal
    half of the signal: `max_gap_seconds`, `gap_ratio` and `integrity`. Omitting
    them is not an error - the synthetic path and any caller that has no clock
    still get the motion verdict - but `integrity` stays `unknown`, because
    "the recording was continuous" is a claim about time and cannot be made
    without it.

    All series must cover the same frames; callers pass coherent slices (the
    synthetic contract validates this, readers slice one episode).
    """
    columns = {name: [float(v) for v in values] for name, values in series.items()}
    lengths = {len(values) for values in columns.values()}
    if len(lengths) > 1:
        raise ValueError("all series must have equal length")
    frame_count = lengths.pop() if lengths else 0

    nonfinite = sum(
        1 for values in columns.values() for value in values if not math.isfinite(value)
    )
    if nonfinite:
        # Stop rather than propagate. A NaN written to `episode_quality` poisons
        # every aggregate that reads it: one corrupt encoder moves the mean
        # movement score of the whole dataset, the monitoring baseline, and every
        # control limit derived from it. Scoring zero and saying why is the only
        # outcome that leaves the rest of the product's arithmetic true.
        return EpisodeQuality(
            frame_count=frame_count,
            movement_score=0.0,
            jerk_score=0.0,
            stall_ratio=0.0,
            verdict="unknown",
            dims=(),
            nonfinite=nonfinite,
            integrity="unknown",
        )

    clock = _clock(timestamps, frame_count)
    if frame_count < MIN_FRAMES or not columns:
        return EpisodeQuality(
            frame_count=frame_count,
            movement_score=0.0,
            jerk_score=0.0,
            stall_ratio=0.0,
            verdict="unknown",
            dims=(),
            max_gap_seconds=clock[0],
            gap_ratio=clock[1],
            integrity="unknown",
        )

    deltas = {name: _deltas(values) for name, values in columns.items()}
    ranges = {name: _range(values) for name, values in columns.items()}

    dims = tuple(
        _dim_quality(name, columns[name], deltas[name], ranges[name]) for name in sorted(columns)
    )
    active = [dim for dim in dims if dim.active]

    # `math.hypot` rather than `sqrt(sum(d*d))`: hypot is specified not to
    # overflow on intermediate results, and a dim whose values reach ~1e160
    # (a raw unnormalised encoder count does not, but a cumulative coordinate can)
    # would otherwise raise OverflowError and fail an ingest over an arithmetic
    # detail that has no bearing on the data being valid.
    l2_per_transition = [
        math.hypot(*(delta[i] for delta in deltas.values())) for i in range(frame_count - 1)
    ]
    movement = sum(l2_per_transition) / len(l2_per_transition)

    jerk = sum(dim.mean_abs_delta_norm for dim in active) / len(active) if active else 0.0

    stalls = sum(
        1
        for i in range(frame_count - 1)
        if all(abs(delta[i]) < ACTIVITY_THRESHOLD * ranges[name] for name, delta in deltas.items())
    )
    stall_ratio = stalls / (frame_count - 1)
    verdict, worst_verdict, worst_dim, judged = _verdicts(dims)

    return EpisodeQuality(
        frame_count=frame_count,
        movement_score=movement,
        jerk_score=jerk,
        stall_ratio=stall_ratio,
        verdict=verdict,
        dims=dims,
        max_gap_seconds=clock[0],
        gap_ratio=clock[1],
        integrity="gapped" if clock[2] else ("ok" if clock[0] is not None else "unknown"),
        worst_verdict=worst_verdict,
        worst_dim=worst_dim,
        judged_dims=judged,
    )


def _clock(
    timestamps: Sequence[float] | None, frame_count: int
) -> tuple[float | None, float, bool]:
    """`(max gap seconds, fraction of gapped transitions, any gap)`.

    The threshold is relative to the median interval rather than an absolute one,
    because a 30 Hz arm and a 1 kHz torque loop are both healthy and a fixed
    millisecond bound would call one of them broken. Median rather than mean
    because a single dropped frame is exactly what this is looking for, and a mean
    inflated by that same drop would raise the bar above every real interval.
    """
    if timestamps is None or frame_count < MIN_FRAMES:
        return None, 0.0, False
    if len(timestamps) != frame_count:
        raise ValueError("timestamps must cover the same frames as the series")
    clock = [float(value) for value in timestamps]
    if not all(math.isfinite(value) for value in clock):
        return None, 0.0, False
    intervals = [right - left for left, right in pairwise(clock)]
    if any(interval < 0 for interval in intervals):
        # A clock that went backwards is not a gap, it is a broken source; the
        # duration is meaningless and every rate derived from it would be a lie.
        return None, 0.0, True
    ordered = sorted(intervals)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return max(intervals), (1.0 if any(ordered) else 0.0), any(ordered)
    threshold = GAP_INTERVAL_FACTOR * median
    gapped = sum(1 for interval in intervals if interval > threshold)
    return max(intervals), gapped / len(intervals), gapped > 0


def _deltas(values: Sequence[float]) -> list[float]:
    return [right - left for left, right in pairwise(values)]


def _range(values: Sequence[float]) -> float:
    span = max(values) - min(values)
    return span if span else 1.0


def _dim_quality(
    name: str, values: Sequence[float], deltas: Sequence[float], dim_range: float
) -> DimQuality:
    unique = len(set(values))
    discrete = unique <= DISCRETE_MAX_UNIQUE
    norm = [abs(delta) / dim_range for delta in deltas]
    sorted_norm = sorted(norm)
    p95 = sorted_norm[max(0, math.ceil(0.95 * len(sorted_norm)) - 1)]
    active = p95 >= ACTIVITY_THRESHOLD

    # The variance is taken over the *normalized* deltas, not the raw ones and then
    # divided. std(d)/r and std(d/r) are the same number for a positive r, so this
    # changes no result - and it removes the squaring of an unnormalized value,
    # which overflows above ~1e154 and failed the whole ingest job over an
    # arithmetic detail. Normalized deltas are bounded by 1, so the sum cannot
    # overflow at all.
    mean = sum(norm) / len(norm)
    variance = sum((value - mean) ** 2 for value in norm) / len(norm)
    return DimQuality(
        name=name,
        active=active,
        discrete=discrete,
        gripper=bool(_GRIPPER.search(name)),
        norm_delta_std=math.sqrt(variance),
        mean_abs_delta_norm=sum(norm) / len(norm),
    )


def _verdicts(dims: Sequence[DimQuality]) -> tuple[str, str, str | None, int]:
    """`(verdict, worst band, which dimension produced it, how many judged)`.

    The verdict is the **median** judged dimension, not the worst one. Worst-case
    was the original rule and it made a single miscalibrated channel out of
    eighteen decide whether an episode was usable - and the operator could not see
    which dimension had done it without opening the JSON. A median needs a majority
    of the arm to be rough, which matches what "this episode is jerky" means to
    someone deciding whether to train on it.

    Nothing is hidden by the change: `worst_verdict` and `worst_dim` are computed
    from the same dimensions and carried on the result, so a caller that genuinely
    wants the strict reading (a robot with one bad encoder *is* a robot with a bad
    encoder) still has it.
    """
    judged = [dim for dim in dims if dim.active and not dim.discrete and not dim.gripper]
    if not judged:
        return "unknown", "unknown", None, 0

    worst = max(judged, key=lambda dim: (dim.norm_delta_std, dim.name))
    sigmas = sorted(dim.norm_delta_std for dim in judged)
    median = sigmas[len(sigmas) // 2]
    return _band(median), _band(worst.norm_delta_std), worst.name, len(judged)


def _band(sigma: float) -> str:
    if sigma >= JERKY_SIGMA:
        return "jerky"
    if sigma >= SMOOTH_SIGMA:
        return "moderate"
    return "smooth"
