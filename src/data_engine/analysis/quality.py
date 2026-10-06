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
#: A dimension is a gripper when its *own field name* says so. The address prefix
#: (an MCAP topic, a LeRobot collection) is deliberately excluded - see `_field`.
_GRIPPER = re.compile("grip", re.IGNORECASE)

MIN_FRAMES = 2

#: A transition whose interval exceeds this multiple of the median interval is a
#: gap: the recorder stopped, the clock jumped, or a message was lost. Median
#: rather than mean, because one enormous gap would otherwise raise the bar above
#: every legitimate interval and hide itself.
GAP_INTERVAL_FACTOR = 5.0

#: How many points the per-episode motion trace may hold, across all its runs.
#: This is a picture of an episode, not a copy of it: 240 points fills the
#: 720-wide chart at a point every 3 px with room for the holes.
TRACE_POINTS = 240

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

    motion_trace: tuple[tuple[tuple[float, float], ...], ...] = ()
    """The motion score over time, as runs of `(seconds since start, score)`.

    A break between runs is a recording gap, drawn as a hole rather than as
    motion across a dropout. Bounded by `TRACE_POINTS` across all runs."""

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
    """Compute motion-quality signals from named per-dimension frame series.

    `timestamps`, when given, must cover the same frames and enables the temporal
    half of the signal: `max_gap_seconds`, `gap_ratio` and `integrity`. Omitting
    them is not an error - the synthetic path and any caller that has no clock
    still get the motion verdict - but `integrity` stays `unknown`, because
    "the recording was continuous" is a claim about time and cannot be made
    without it.

    `max_interval_seconds` is for the streaming reader, which keeps a bounded
    window of a log far too long to hold in memory. Its timestamps describe only
    the retained window, so a drop that happened an hour ago is invisible to
    them; the reader can still know the largest interval it ever saw, exactly and
    in constant memory, and passes it here. It raises `max_gap_seconds` and can
    make `integrity` `gapped`, but it cannot raise `gap_ratio` - a ratio over a
    window is not a ratio over the log, and reporting one as the other would be
    the same class of lie this function exists to prevent.

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
    widest, ratio, gapped, threshold = clock
    if (
        max_interval_seconds is not None
        and math.isfinite(max_interval_seconds)
        and max_interval_seconds > (widest or 0.0)
    ):
        # A drop the retained window cannot see still happened, and "the recording
        # was continuous" has to mean the whole recording.
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
    """`(max gap seconds, fraction of gapped transitions, any gap, threshold)`.

    The threshold is relative to the median interval rather than an absolute one,
    because a 30 Hz arm and a 1 kHz torque loop are both healthy and a fixed
    millisecond bound would call one of them broken. Median rather than mean
    because a single dropped frame is exactly what this is looking for, and a mean
    inflated by that same drop would raise the bar above every real interval.
    """
    if timestamps is None or frame_count < MIN_FRAMES:
        return None, 0.0, False, 0.0
    if len(timestamps) != frame_count:
        raise ValueError("timestamps must cover the same frames as the series")
    clock = [float(value) for value in timestamps]
    if not all(math.isfinite(value) for value in clock):
        return None, 0.0, False, 0.0
    intervals = [right - left for left, right in pairwise(clock)]
    if any(interval < 0 for interval in intervals):
        # A clock that went backwards is not a gap, it is a broken source; the
        # duration is meaningless and every rate derived from it would be a lie.
        return None, 0.0, True, 0.0
    ordered = sorted(intervals)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return max(intervals), (1.0 if any(ordered) else 0.0), any(ordered), 0.0
    threshold = GAP_INTERVAL_FACTOR * median
    gapped = sum(1 for interval in intervals if interval > threshold)
    return max(intervals), gapped / len(intervals), gapped > 0, threshold


def _field(name: str) -> str:
    """The dimension's own field name, with its address prefix removed.

    Dimension names are *addresses*: the MCAP reader names them `<topic>.<path>`
    and the LeRobot reader `<feature>[<index>]`. A topic is where a signal came
    from, not what it is, so nothing that classifies a dimension may look at it.
    The first version matched the gripper exclusion against the whole address,
    and a bimanual rig whose busiest joint topic was `/left/gripper/joint_states`
    had every one of its joint dimensions excluded at once: `judged_dims` fell to
    zero and the episode's motion verdict was reported as `unknown` even though
    nothing was wrong with the data. Byte-identical payloads came out `smooth` as
    `/joint_states` and `unknown` as `/left/gripper/joint_states` (EXP-0014 D1).

    Only the last dotted segment participates, so `...joint_states.position[0]`
    is classified by `position[0]`, while a payload that literally names a field
    `gripper` still is one. `observation.state[3]` is classified by `state[3]`,
    its own field name with the collection prefix dropped. A continuous gripper
    channel is a real motion signal once the topic
    stops vetoing it; the binary gripper the exclusion exists for is still caught
    by the discrete-dimension rule (`<= DISCRETE_MAX_UNIQUE` values).
    """
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
        gripper=bool(_GRIPPER.search(_field(name))),
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
    """The motion score over time: one point per transition, holes at dropouts.

    Each point is the judging dimensions' mean `|delta| / range` for that
    transition - the very quantity `jerk_score` averages, so the chart and the
    score cannot disagree about what motion is - placed at the frame the motion
    was observed and expressed in seconds since the episode's first frame.

    A transition that spans a dropout is not motion: `|delta|` across a 30 s
    hole is two poses, not a velocity. Such transitions are not drawn, so the
    hole between runs *is* the recording gap, rendered as what it was.

    No trace without a trustworthy clock (`threshold <= 0` covers no timestamps,
    a backwards clock, and a zero-width interval scale): the x axis would be
    fiction. `active` dimensions judge the score; an episode with none (all
    grippers, all discrete) falls back to every dimension so the picture still
    exists and says so by being flat where motion was inert.
    """
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
    """Downsample runs to `cap` points total, keeping every run's endpoints.

    Endpoints are what draw the holes, so thinning must never move them. When
    even two points per run does not fit, the shortest runs go entirely: losing
    a small run widens a hole the recording already had, while merging runs
    would draw a line across a gap that never was.
    """
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
