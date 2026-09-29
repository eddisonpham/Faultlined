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
SMOOTH_SIGMA = 0.02
JERKY_SIGMA = 0.1
_GRIPPER = re.compile("grip", re.IGNORECASE)

MIN_FRAMES = 2


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_count": self.frame_count,
            "movement_score": self.movement_score,
            "jerk_score": self.jerk_score,
            "stall_ratio": self.stall_ratio,
            "verdict": self.verdict,
            "dims": [dim.to_dict() for dim in self.dims],
        }


def analyze(series: Mapping[str, Sequence[float]]) -> EpisodeQuality:
    """Compute motion-quality signals from named per-dimension frame series.

    All series must cover the same frames; callers pass coherent slices (the
    synthetic contract validates this, readers slice one episode).
    """
    columns = {name: [float(v) for v in values] for name, values in series.items()}
    lengths = {len(values) for values in columns.values()}
    if len(lengths) > 1:
        raise ValueError("all series must have equal length")
    frame_count = lengths.pop() if lengths else 0
    if frame_count < MIN_FRAMES or not columns:
        return EpisodeQuality(
            frame_count=frame_count,
            movement_score=0.0,
            jerk_score=0.0,
            stall_ratio=0.0,
            verdict="unknown",
            dims=(),
        )

    deltas = {name: _deltas(values) for name, values in columns.items()}
    ranges = {name: _range(values) for name, values in columns.items()}

    dims = tuple(
        _dim_quality(name, columns[name], deltas[name], ranges[name]) for name in sorted(columns)
    )
    active = [dim for dim in dims if dim.active]

    l2_per_transition = [
        math.sqrt(sum(delta[i] ** 2 for delta in deltas.values())) for i in range(frame_count - 1)
    ]
    movement = sum(l2_per_transition) / len(l2_per_transition)

    if active:
        jerk = sum(dim.mean_abs_delta_norm for dim in active) / len(active)
    else:
        jerk = 0.0

    stalls = sum(
        1
        for i in range(frame_count - 1)
        if all(abs(delta[i]) < ACTIVITY_THRESHOLD * ranges[name] for name, delta in deltas.items())
    )
    stall_ratio = stalls / (frame_count - 1)

    return EpisodeQuality(
        frame_count=frame_count,
        movement_score=movement,
        jerk_score=jerk,
        stall_ratio=stall_ratio,
        verdict=_verdict(dims),
        dims=dims,
    )


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
    mean = sum(deltas) / len(deltas)
    variance = sum((delta - mean) ** 2 for delta in deltas) / len(deltas)
    return DimQuality(
        name=name,
        active=active,
        discrete=discrete,
        gripper=bool(_GRIPPER.search(name)),
        norm_delta_std=math.sqrt(variance) / dim_range,
        mean_abs_delta_norm=sum(norm) / len(norm),
    )


def _verdict(dims: Sequence[DimQuality]) -> str:
    """Worst absolute band among judged dims (active, non-discrete, non-gripper)."""
    judged = [dim for dim in dims if dim.active and not dim.discrete and not dim.gripper]
    if not judged:
        return "unknown"
    if any(dim.norm_delta_std >= JERKY_SIGMA for dim in judged):
        return "jerky"
    if any(dim.norm_delta_std >= SMOOTH_SIGMA for dim in judged):
        return "moderate"
    return "smooth"
