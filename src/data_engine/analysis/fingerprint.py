"""Behavioural fingerprints: redundancy from signals ingest already computed (ADR 0032)."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from data_engine.analysis.quality import field_name

__all__ = [
    "DEFAULT_THRESHOLD",
    "MAX_EPISODES",
    "Duplicate",
    "Fingerprint",
    "RedundancyGroup",
    "RedundancyReport",
    "distance",
    "fingerprint",
    "redundancy_report",
]

SHAPE_POINTS = 32

COMPONENT_WEIGHTS: dict[str, float] = {"shape": 0.5, "dynamics": 0.35, "temporal": 0.15}

DEFAULT_THRESHOLD = 0.04

MAX_EPISODES = 500

_EPSILON = 1e-9

_VERDICT_RANK: dict[str, int] = {"smooth": 0, "moderate": 1, "jerky": 2, "unknown": 3}


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One episode's behavioural descriptor, derived entirely from persisted signals."""

    episode_id: str
    label: str
    """Human-readable name for a report row: the episode's task string, else its id."""

    verdict: str
    judged_dims: int
    stall_ratio: float
    gap_ratio: float
    shape: tuple[float, ...]
    """The motion trace on a normalised `[0, 1]` time axis, divided by its own mean. """

    dynamics: tuple[tuple[str, float, float], ...]
    """`(field, norm_delta_std, mean_abs_delta_norm)` per judged dimension, sorted by field."""

    @property
    def comparable(self) -> bool:
        """Whether this episode can be compared to another at all."""
        return bool(self.shape) or bool(self.dynamics)


@dataclass(frozen=True, slots=True)
class Duplicate:
    """One episode a report would collapse into its group's representative."""

    episode_id: str
    label: str
    distance: float
    reason: str
    """The component that contributed most to the distance: shape, dynamics or temporal."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "label": self.label,
            "distance": self.distance,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RedundancyGroup:
    """A kept episode and the episodes that are near-duplicates of it."""

    representative: str
    label: str
    duplicates: tuple[Duplicate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "representative": self.representative,
            "label": self.label,
            "duplicates": [item.to_dict() for item in self.duplicates],
        }


@dataclass(frozen=True, slots=True)
class RedundancyReport:
    """What a set of episodes looks like once near-duplicates are collapsed."""

    threshold: float
    episode_count: int
    compared_count: int
    incomparable: tuple[str, ...]
    groups: tuple[RedundancyGroup, ...]
    truncated: bool

    @property
    def distinct_count(self) -> int:
        """Behaviours held: kept reps + episodes with no comparison (each = distinct)."""

        return self.episode_count - self.redundant_count

    @property
    def redundant_count(self) -> int:
        return sum(len(group.duplicates) for group in self.groups)

    @property
    def reduction_ratio(self) -> float:
        if not self.episode_count:
            return 0.0
        return self.redundant_count / self.episode_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "behavioural-fingerprint-v1",
            "threshold": self.threshold,
            "components": dict(COMPONENT_WEIGHTS),
            "episode_count": self.episode_count,
            "compared_count": self.compared_count,
            "distinct_count": self.distinct_count,
            "redundant_count": self.redundant_count,
            "reduction_ratio": self.reduction_ratio,
            "incomparable": list(self.incomparable),
            "groups": [group.to_dict() for group in self.groups],
            "truncated": self.truncated,
        }


def fingerprint(row: Mapping[str, Any]) -> Fingerprint:
    """Build one fingerprint from an `episode_fingerprint_inputs` row (ADR 0032)."""
    episode_id = str(row.get("id") or "")
    task = str(row.get("task") or "").strip()
    return Fingerprint(
        episode_id=episode_id,
        label=task or episode_id,
        verdict=str(row.get("verdict") or "unknown"),
        judged_dims=_count(row.get("judged_dims")),
        stall_ratio=_fraction(row.get("stall_ratio")),
        gap_ratio=_fraction(row.get("gap_ratio")),
        shape=_shape(row.get("motion_trace")),
        dynamics=_dynamics(row.get("dims")),
    )


def distance(first: Fingerprint, second: Fingerprint) -> float | None:
    """Weighted mean of the components both episodes have, or `None` when they share none."""
    parts = _parts(first, second)
    return None if parts is None else _score(parts)


def redundancy_report(
    episodes: Iterable[Mapping[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_episodes: int = MAX_EPISODES,
) -> RedundancyReport:
    """Collapse near-duplicates in one deterministic pass."""
    ranked = [fingerprint(row) for row in episodes]
    truncated = len(ranked) > max_episodes
    if truncated:
        ranked = ranked[:max_episodes]

    incomparable: list[str] = []
    kept: list[Fingerprint] = []
    kept_dynamics: list[dict[str, tuple[float, float]]] = []
    duplicates: dict[str, list[Duplicate]] = {}

    for candidate in sorted(ranked, key=_rank_key):
        if not candidate.comparable:
            incomparable.append(candidate.episode_id)
            continue
        candidate_dynamics = _dynamics_map(candidate)
        match: Fingerprint | None = None
        match_score = math.inf
        match_reason = ""
        for representative, representative_dynamics in zip(kept, kept_dynamics, strict=True):
            found = _match(candidate, representative, representative_dynamics, threshold)
            if found is None:
                continue
            score, reason = found
            if score < match_score:
                match, match_score, match_reason = representative, score, reason
        if match is None:
            kept.append(candidate)
            kept_dynamics.append(candidate_dynamics)
            continue
        duplicates.setdefault(match.episode_id, []).append(
            Duplicate(
                episode_id=candidate.episode_id,
                label=candidate.label,
                distance=match_score,
                reason=match_reason,
            )
        )

    groups = tuple(
        RedundancyGroup(
            representative=representative.episode_id,
            label=representative.label,
            duplicates=tuple(duplicates[representative.episode_id]),
        )
        for representative in kept
        if duplicates.get(representative.episode_id)
    )
    return RedundancyReport(
        threshold=threshold,
        episode_count=len(ranked),
        compared_count=len(ranked) - len(incomparable),
        incomparable=tuple(incomparable),
        groups=groups,
        truncated=truncated,
    )


def _dynamics_map(item: Fingerprint) -> dict[str, tuple[float, float]]:
    return {name: (nstd, mad) for name, nstd, mad in item.dynamics}


_TOTAL_WEIGHT = sum(COMPONENT_WEIGHTS.values())


def _match(
    candidate: Fingerprint,
    representative: Fingerprint,
    representative_dynamics: Mapping[str, tuple[float, float]],
    threshold: float,
) -> tuple[float, str] | None:
    """`(distance, dominant component)` when the pair is within `threshold`, else `None`."""
    budget = threshold * _TOTAL_WEIGHT
    weighted = 0.0
    present = 0.0
    parts: list[tuple[str, float]] = []
    has_shape = False
    has_dynamics = False

    temporal = (
        abs(candidate.stall_ratio - representative.stall_ratio)
        + abs(candidate.gap_ratio - representative.gap_ratio)
    ) / 2
    if COMPONENT_WEIGHTS["temporal"] * temporal > budget:
        return None
    weighted += COMPONENT_WEIGHTS["temporal"] * temporal
    present += COMPONENT_WEIGHTS["temporal"]
    parts.append(("temporal", temporal))

    total = 0.0
    shared = 0
    for name, std, delta in candidate.dynamics:
        other = representative_dynamics.get(name)
        if other is None:
            continue
        total += (abs(std - other[0]) + abs(delta - other[1])) / 2
        shared += 1
    if shared:
        dynamics = total / shared
        if COMPONENT_WEIGHTS["dynamics"] * dynamics > budget:
            return None
        weighted += COMPONENT_WEIGHTS["dynamics"] * dynamics
        present += COMPONENT_WEIGHTS["dynamics"]
        parts.append(("dynamics", dynamics))
        has_dynamics = True

    if candidate.shape and representative.shape:
        span = len(candidate.shape)
        limit = budget * span / COMPONENT_WEIGHTS["shape"]
        accumulated = 0.0
        for left, right in zip(candidate.shape, representative.shape, strict=True):
            accumulated += abs(left - right)
            if accumulated > limit:
                return None
        shape = accumulated / span
        if COMPONENT_WEIGHTS["shape"] * shape > budget:
            return None
        weighted += COMPONENT_WEIGHTS["shape"] * shape
        present += COMPONENT_WEIGHTS["shape"]
        parts.append(("shape", shape))
        has_shape = True

    if not (has_shape or has_dynamics) or present <= 0:
        return None
    score = weighted / present
    if score > threshold:
        return None
    return score, _dominant(dict(parts))


def _rank_key(item: Fingerprint) -> tuple[int, float, int, str]:
    return (
        _VERDICT_RANK.get(item.verdict, 3),
        item.stall_ratio,
        -item.judged_dims,
        item.episode_id,
    )


def _parts(first: Fingerprint, second: Fingerprint) -> dict[str, float] | None:
    """Per-component distances over what two fingerprints have in common, or `None` when that is"""

    parts: dict[str, float] = {}
    if first.shape and second.shape:
        parts["shape"] = _mean_abs(first.shape, second.shape)
    shared = {name: (nstd, mad) for name, nstd, mad in second.dynamics}
    overlap = [
        (nstd, mad, shared[name][0], shared[name][1])
        for name, nstd, mad in first.dynamics
        if name in shared
    ]
    if overlap:
        parts["dynamics"] = sum(
            (abs(left_nstd - right_nstd) + abs(left_mad - right_mad)) / 2
            for left_nstd, left_mad, right_nstd, right_mad in overlap
        ) / len(overlap)
    temporal = (
        abs(first.stall_ratio - second.stall_ratio) + abs(first.gap_ratio - second.gap_ratio)
    ) / 2
    parts["temporal"] = temporal
    if "shape" not in parts and "dynamics" not in parts:
        return None
    return parts


def _score(parts: Mapping[str, float]) -> float:
    total = sum(COMPONENT_WEIGHTS[name] for name in parts)
    return sum(COMPONENT_WEIGHTS[name] * value for name, value in parts.items()) / total


def _dominant(parts: Mapping[str, float]) -> str:
    return max(parts, key=lambda name: COMPONENT_WEIGHTS[name] * parts[name])


def _mean_abs(left: Sequence[float], right: Sequence[float]) -> float:
    total = sum(abs(a - b) for a, b in zip(left, right, strict=True))
    return total / len(left)


def _fraction(value: Any) -> float:
    """A fraction, or 0.0 for anything that is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    number = float(value)
    return int(number) if math.isfinite(number) else 0


def _dynamics(raw: Any) -> tuple[tuple[str, float, float], ...]:
    """Per-judged-dimension motion character, keyed by the dimension's own field name."""
    if not isinstance(raw, Sequence):
        return ()
    collected: dict[str, tuple[float, float]] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("active") or entry.get("discrete"):
            continue
        name = field_name(str(entry.get("name") or ""))
        if not name:
            continue
        collected[name] = (
            _fraction(entry.get("norm_delta_std")),
            _fraction(entry.get("mean_abs_delta_norm")),
        )
    return tuple((name, motion[0], motion[1]) for name, motion in sorted(collected.items()))


def _shape(raw: Any) -> tuple[float, ...]:
    """The motion trace resampled onto a normalised time axis and divided by its own mean."""
    points = _points(raw)
    if len(points) < 2:
        return ()
    start = points[0][0]
    span = points[-1][0] - start
    if span <= _EPSILON:
        return ()
    times = [point[0] for point in points]
    sampled = tuple(
        _interpolate(points, times, start + span * index / (SHAPE_POINTS - 1))
        for index in range(SHAPE_POINTS)
    )
    magnitude = sum(abs(value) for value in sampled) / len(sampled)
    if magnitude <= _EPSILON:
        return ()
    return tuple(value / magnitude for value in sampled)


def _points(raw: Any) -> list[tuple[float, float]]:
    """Flatten the trace's runs into one chronological series, dropping anything unusable."""
    if not isinstance(raw, Sequence):
        return []
    points: list[tuple[float, float]] = []
    for run in raw:
        if not isinstance(run, Sequence):
            continue
        for pair in run:
            if not isinstance(pair, Sequence) or len(pair) != 2:
                continue
            seconds, value = pair[0], pair[1]
            if isinstance(seconds, bool) or isinstance(value, bool):
                continue
            if not isinstance(seconds, (int, float)) or not isinstance(value, (int, float)):
                continue
            if not (math.isfinite(float(seconds)) and math.isfinite(float(value))):
                continue
            points.append((float(seconds), float(value)))
    points.sort()
    return points


def _interpolate(points: Sequence[tuple[float, float]], times: Sequence[float], at: float) -> float:
    index = bisect_right(times, at)
    if index == 0:
        return points[0][1]
    if index >= len(points):
        return points[-1][1]
    left, right = points[index - 1], points[index]
    width = right[0] - left[0]
    if width <= _EPSILON:
        return right[1]
    return left[1] + (right[1] - left[1]) * ((at - left[0]) / width)
