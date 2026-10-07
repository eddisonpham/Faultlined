"""Behavioural fingerprints: redundancy from signals ingest already computed (ADR 0032).

An episode's fingerprint is assembled entirely from what ``analyze()`` computed at ingest
(ADR 0018) and ``record_episode_quality`` persisted: the motion trace, the per-dimension
motion character, and the temporal fractions. Nothing here reads an artifact, opens a frame,
or imports a numerical library.

The industry answer to "are these two episodes the same behaviour recorded twice" is an
embedding: SemDeDup removes pairs above a cosine-similarity threshold in a pretrained model's
space, and FiftyOne Brain computes near-duplicates, uniqueness and representativeness the same
way. That is closed to this engine by decision, not by accident - the runtime dependency set
has no numerical library at all, frames are never in memory (an artifact is the Parquet file
or the bag), and ADR 0020 rejected a learned, non-reproducible verdict in a path a human is
asked to trust.

What is traded away is detection power; what is bought is that curation becomes a pure
function of what ingest already wrote. Three consequences follow, and they are the reason this
module exists rather than a batch job:

- **Deterministic.** The same catalog produces byte-identical reports, so a report can be
  compared between two builds and cited the way a build hash is.
- **Explainable.** A pair is not "similar"; it is close because the weighted mean of the
  components *both episodes have* was small, and the report names which component dominated.
- **Free.** No model pass, no GPU, no artifact read, no new dependency, and it is available the
  moment an episode exists rather than after someone remembers to run a job.

The measure is deliberately narrow. ``shape`` compares the motion trace resampled onto a
normalised time axis, so it is insensitive to duration and to a constant scale factor;
``dynamics`` compares per-dimension normalised motion, so units and robot scale cancel;
``temporal`` compares stall and gap *fractions*, not durations. An episode ingested without a
clock has no trace and therefore no shape, and it says so: the distance is a weighted mean over
the components both sides have, renormalised by the weights present, never a sum with a missing
component scored as zero - that would make "we could not compare this" indistinguishable from
"these are identical".
"""

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

#: Points the motion trace is resampled to. The trace itself holds up to 240 points
#: (`quality.TRACE_POINTS`); 32 is enough to separate a smooth reach from a stutter without
#: making the pairwise comparison the dominant cost of a report over thousands of episodes.
SHAPE_POINTS = 32

#: Component weights. Shape dominates because it is the only component that sees the episode
#: as a curve rather than as a summary; temporal character is the weakest evidence on its own
#: (two unrelated takes can stall similarly) and is weighted accordingly. The weights matter
#: only relatively - the distance is renormalised over whichever components are present.
COMPONENT_WEIGHTS: dict[str, float] = {"shape": 0.5, "dynamics": 0.35, "temporal": 0.15}

#: Calibrated, not chosen. `scripts/fingerprint_calibration.py` plants re-recordings and
#: measures the sweep; at 0.04 it reports pairwise precision 0.906 with recall 0.400 against
#: 0.095 precision at the 0.10 this shipped with first (EXP-0019). The operating point is
#: chosen for precision: a false merge asserts that two episodes are one behaviour and hides
#: the distinct one, while a missed duplicate only leaves redundancy the operator can still
#: see. Re-run that script if `episode_quality` ever changes shape.
DEFAULT_THRESHOLD = 0.04

#: The most episodes one report will score. The pairwise scan is the cost, and it is
#: superlinear: measured on a throwaway catalog of planted re-recordings (EXP-0019), the whole
#: read-plus-score is 142 ms at 300 episodes, 218 ms at 500, 618 ms at 1 000 and 1 815 ms at
#: 2 000 - enough that the 2 000 this shipped with first would have been a two-second page for
#: the largest builds. 500 keeps the page inside a quarter second. Those figures are the
#: *worst* case for the algorithm (almost every episode distinct, so almost every kept episode
#: is still a representative to compare against); a genuinely redundant build collapses early
#: and costs less. A truncated report says so rather than quietly reporting a fraction of a
#: build as if it were the whole.
MAX_EPISODES = 500

_EPSILON = 1e-9

#: Which verdict makes the better representative of a group. A clean demonstration is the one
#: worth keeping when its group is collapsed, so `smooth` sorts first; an unjudged episode is
#: never preferred over a judged one.
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
    """The motion trace on a normalised `[0, 1]` time axis, divided by its own mean. Empty
    when the episode was ingested without a clock - the trace's x axis would be fiction."""

    dynamics: tuple[tuple[str, float, float], ...]
    """`(field, norm_delta_std, mean_abs_delta_norm)` per judged dimension, sorted by field."""

    @property
    def comparable(self) -> bool:
        """Whether this episode can be compared to another at all.

        Temporal fractions alone are not evidence: two unrelated takes stall similarly all the
        time. A fingerprint with neither a trace nor any judged dimension is reported as
        incomparable rather than scored against everything else and called distinct by default.
        """
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
    """What a set of episodes looks like once near-duplicates are collapsed.

    Read-only by construction: nothing is deleted, quarantined or excluded. The report is an
    input to a curation decision, and the decision belongs to the operator.
    """

    threshold: float
    episode_count: int
    compared_count: int
    incomparable: tuple[str, ...]
    groups: tuple[RedundancyGroup, ...]
    truncated: bool

    @property
    def distinct_count(self) -> int:
        """How many behaviours the set holds: kept representatives, plus episodes that could
        not be compared to anything (counting those as distinct is the honest direction - we
        have no evidence they repeat)."""
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
    """Build one fingerprint from an `episode_fingerprint_inputs` row (ADR 0032).

    Never raises on a malformed row: a missing or non-finite signal is recorded as an absent
    one, the same way `analyze()` refuses to let one bad value poison a dataset statistic
    (ADR 0023). A fingerprint that cannot be built is a fingerprint with fewer components, and
    the report says so by comparing on what is left.
    """
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
    """Weighted mean of the components both episodes have, or `None` when they share none.

    The renormalisation is the point. A component one side is missing is left out of both the
    numerator and the denominator, so an episode without a clock is compared on dynamics and
    temporal character alone rather than being pushed away from everything by a phantom zero.
    """
    parts = _parts(first, second)
    return None if parts is None else _score(parts)


def redundancy_report(
    episodes: Iterable[Mapping[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_episodes: int = MAX_EPISODES,
) -> RedundancyReport:
    """Collapse near-duplicates in one deterministic pass.

    The shape is SemDeDup's - walk the items, keep one, attach anything close to a kept one -
    with two changes that make it this engine's rather than that paper's. The walk order is an
    explainable quality rank ("keep the best take": verdict, then less stalling, then more
    judged dimensions, then id) instead of a model score; and the *reason* a pair was collapsed
    travels with it, so an operator can disagree with the report without re-deriving it.

    Input order is preserved for truncation only: a caller passing a build's members gets the
    first `max_episodes` in membership order and `truncated=True`, rather than a silently
    sampled subset.

    The pairwise scan is the cost, so `_match` abandons a pair as soon as one component alone
    is enough to exceed the threshold. That is a *sound* shortcut - it can only skip pairs
    whose weighted mean is already above the threshold - and it pays off exactly where it is
    needed: a catalog of genuinely distinct episodes prunes almost everything after the two
    cheapest components, while a catalog full of duplicates collapses early and therefore has
    few representatives left to compare against.
    """
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


#: Sum of the component weights. The early exits below bound the distance with a single
#: component against this total rather than against the weights present, because a pair's
#: comparable components are a subset of all of them and the subset's weight can only be
#: smaller - which makes the bound conservative and therefore sound whatever the weights are.
_TOTAL_WEIGHT = sum(COMPONENT_WEIGHTS.values())


def _match(
    candidate: Fingerprint,
    representative: Fingerprint,
    representative_dynamics: Mapping[str, tuple[float, float]],
    threshold: float,
) -> tuple[float, str] | None:
    """`(distance, dominant component)` when the pair is within `threshold`, else `None`.

    Components are computed cheapest first - two fractions, then the per-dimension overlap,
    then the 32-point shape - and any one of them alone exceeding the threshold ends the pair,
    because a weighted mean is at least each component's own weighted share of it. The shape
    loop carries the same test inside the vector, so a pair that diverges early is abandoned
    before the remaining points are read.

    Semantics are identical to `distance()`, and the tests pin that: the shortcut is an
    optimisation, never a second definition of the measure.
    """
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
    """Per-component distances over what two fingerprints have in common, or `None` when that
    is nothing worth comparing on."""
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
    """A fraction, or 0.0 for anything that is not a finite number.

    A non-finite value reaching a comparison would make every distance involving it a NaN and
    every `<= threshold` test false, which is the failure ADR 0023 exists to prevent one layer
    up. Here it is 0.0 and the component carries no information, which is visible in the
    report rather than silently poisoning it.
    """
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
    """Per-judged-dimension motion character, keyed by the dimension's own field name.

    Discrete dimensions are dropped: a binary gripper's normalised delta is raw noise, and
    ADR 0018 already excludes it from the verdict for the same reason. `field_name` is
    `analysis.quality`'s own rule (the ADR 0018 amendment) rather than a second copy of it, so
    a change to what a dimension's name *is* cannot make the two disagree.
    """
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
    """The motion trace resampled onto a normalised time axis and divided by its own mean.

    Two properties are wanted and both come from the normalisation. Dividing by the episode's
    own *magnitude* makes the curve scale-free, so the same motion performed faster or slower,
    or with a different amount of overall travel, produces the same vector. Resampling onto
    `t / t_max` makes it duration-free, so a 6 s demonstration and a 60 s one of the same shape
    compare.

    The normaliser is the mean of the *absolute* values, not the mean: `quality._trace`
    produces a non-negative signal (it sums absolute per-dimension deltas), where the two are
    the same number, but a caller handing this a zero-mean series would otherwise divide by a
    near-zero denominator and get a vector of enormous, meaningless spikes.
    """
    points = _points(raw)
    if len(points) < 2:
        return ()
    start = points[0][0]
    span = points[-1][0] - start
    if span <= _EPSILON:
        return ()
    # The time index is built once and walked, not rebuilt per sample point. It is the only
    # per-episode cost in the module (everything else is pairwise), and the rebuild is not
    # free: 500 traces of 240 points took 89 ms with the per-point rebuild and 40 ms with the
    # index hoisted, so hoisting buys about 50 ms of a 500-episode report's scoring pass.
    times = [point[0] for point in points]
    sampled = tuple(
        _interpolate(points, times, start + span * index / (SHAPE_POINTS - 1))
        for index in range(SHAPE_POINTS)
    )
    magnitude = sum(abs(value) for value in sampled) / len(sampled)
    if magnitude <= _EPSILON:
        # An episode whose trace is uniformly zero has no shape; a vector of zeros would have
        # a distance of zero to every other flat episode, which is a false duplicate.
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
