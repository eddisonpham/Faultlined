"""Episode motion-quality analysis (ADR 0018)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

ACTIVITY_THRESHOLD = 0.001
DISCRETE_MAX_UNIQUE = 4
_GRIPPER = re.compile("grip", re.IGNORECASE)

MIN_FRAMES = 2

GAP_INTERVAL_FACTOR = 5.0

TRACE_POINTS = 240

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
    """How many values were NaN or infinite. """

    max_gap_seconds: float | None = None
    """Longest interval between consecutive samples. """

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

    motion_trace: tuple[tuple[tuple[float, float], ...], ...] = ()
    """The motion score over time, as runs of `(seconds since start, score)`."""

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
            "motion_trace": [[[t, v] for t, v in run] for run in self.motion_trace],
        }


def analyze(
    series: Mapping[str, Sequence[float]],
    *,
    timestamps: Sequence[float] | None = None,
    max_interval_seconds: float | None = None,
) -> EpisodeQuality:
    """Compute motion-quality signals from named per-dimension frame series."""
    columns = {name: [float(v) for v in values] for name, values in series.items()}
    lengths = {len(values) for values in columns.values()}
    if len(lengths) > 1:
        raise ValueError("all series must have equal length")
    frame_count = lengths.pop() if lengths else 0

    nonfinite = sum(
        1 for values in columns.values() for value in values if not math.isfinite(value)
    )
    if nonfinite:
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
    widest, ratio, gapped, threshold = clock
    if (
        max_interval_seconds is not None
        and math.isfinite(max_interval_seconds)
        and max_interval_seconds > (widest or 0.0)
    ):
        widest = max_interval_seconds
        gapped = gapped or max_interval_seconds > threshold
    if frame_count < MIN_FRAMES or not columns:
        return EpisodeQuality(
            frame_count=frame_count,
            movement_score=0.0,
            jerk_score=0.0,
            stall_ratio=0.0,
            verdict="unknown",
            dims=(),
            max_gap_seconds=widest,
            gap_ratio=ratio,
            integrity="unknown",
        )

    deltas = {name: _deltas(values) for name, values in columns.items()}
    ranges = {name: _range(values) for name, values in columns.items()}

    dims = tuple(
        _dim_quality(name, columns[name], deltas[name], ranges[name]) for name in sorted(columns)
    )
    active = [dim for dim in dims if dim.active]

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
        max_gap_seconds=widest,
        gap_ratio=ratio,
        integrity="gapped" if gapped else ("ok" if widest is not None else "unknown"),
        worst_verdict=worst_verdict,
        worst_dim=worst_dim,
        judged_dims=judged,
        motion_trace=_trace(timestamps, deltas, ranges, active, threshold),
    )


def _clock(
    timestamps: Sequence[float] | None, frame_count: int
) -> tuple[float | None, float, bool, float]:
    """`(max gap seconds, fraction of gapped transitions, any gap, threshold)`."""
    if timestamps is None or frame_count < MIN_FRAMES:
        return None, 0.0, False, 0.0
    if len(timestamps) != frame_count:
        raise ValueError("timestamps must cover the same frames as the series")
    clock = [float(value) for value in timestamps]
    if not all(math.isfinite(value) for value in clock):
        return None, 0.0, False, 0.0
    intervals = [right - left for left, right in pairwise(clock)]
    if any(interval < 0 for interval in intervals):
        return None, 0.0, True, 0.0
    ordered = sorted(intervals)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return max(intervals), (1.0 if any(ordered) else 0.0), any(ordered), 0.0
    threshold = GAP_INTERVAL_FACTOR * median
    gapped = sum(1 for interval in intervals if interval > threshold)
    return max(intervals), gapped / len(intervals), gapped > 0, threshold


def field_name(name: str) -> str:
    """The dimension's own field name, with its address prefix removed."""
    return name.rsplit(".", 1)[-1]


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

    mean = sum(norm) / len(norm)
    variance = sum((value - mean) ** 2 for value in norm) / len(norm)
    return DimQuality(
        name=name,
        active=active,
        discrete=discrete,
        gripper=bool(_GRIPPER.search(field_name(name))),
        norm_delta_std=math.sqrt(variance),
        mean_abs_delta_norm=sum(norm) / len(norm),
    )


def _trace(
    timestamps: Sequence[float] | None,
    deltas: Mapping[str, Sequence[float]],
    ranges: Mapping[str, float],
    active: Sequence[DimQuality],
    threshold: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """The motion score over time: one point per transition, holes at dropouts."""
    if timestamps is None or threshold <= 0.0:
        return ()
    names = [dim.name for dim in active] or sorted(ranges)
    if not names:
        return ()
    start = float(timestamps[0])
    runs: list[list[tuple[float, float]]] = [[]]
    for i in range(len(timestamps) - 1):
        if float(timestamps[i + 1]) - float(timestamps[i]) > threshold:
            if runs[-1]:
                runs.append([])
            continue
        value = sum(abs(deltas[name][i]) / ranges[name] for name in names) / len(names)
        runs[-1].append((float(timestamps[i + 1]) - start, value))
    filled = [run for run in runs if run]
    return tuple(tuple(run) for run in _thin(filled, TRACE_POINTS))


def _thin(runs: list[list[tuple[float, float]]], cap: int) -> list[list[tuple[float, float]]]:
    """Downsample runs to `cap` points total, keeping every run's endpoints."""
    keeps = [len(run) for run in runs]
    while sum(keeps) > cap:
        fattest = max(range(len(keeps)), key=lambda i: keeps[i])
        if keeps[fattest] <= 2:
            break
        keeps[fattest] -= 1
    if sum(keeps) > cap:
        order = sorted(range(len(runs)), key=lambda i: (len(runs[i]), i), reverse=True)
        kept = set()
        total = 0
        for i in order:
            if total + keeps[i] <= cap:
                kept.add(i)
                total += keeps[i]
        runs = [runs[i] for i in sorted(kept)]
        keeps = [keeps[i] for i in sorted(kept)]
    thinned: list[list[tuple[float, float]]] = []
    for run, keep in zip(runs, keeps, strict=True):
        if keep >= len(run):
            thinned.append(run)
        elif keep <= 2:
            thinned.append([run[0], run[-1]])
        else:
            stride = (len(run) - 1) / (keep - 1)
            thinned.append([run[round(step * stride)] for step in range(keep)])
    return thinned


def _verdicts(dims: Sequence[DimQuality]) -> tuple[str, str, str | None, int]:
    """`(verdict, worst band, which dimension produced it, how many judged)`."""
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
