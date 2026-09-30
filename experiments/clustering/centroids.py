"""How a centroid should move when a point joins its cluster.

This is resolved before the main factorial, deliberately. The update rule *is*
the algorithm in an online setting: with a batch method the centroid is a mean of
a fixed set, but with an online method every point shifts the centre that decides
where the next point goes, so a bad rule compounds instead of averaging out.
Leaving it inside the factorial would multiply the search by a factor nobody
needs and make the results unreadable.

Two jobs are being asked of a centroid, and they want different things:

- **Geometry** - which cluster does the next point join? A robust vector, because
  one bad point must not be able to drag a cluster.
- **Display** - what do we show a human? A real member string, not a vector
  nobody can read. That is the medoid's job, and it is deliberately not the same
  object as the assignment centroid.

The rules fall into two families. Streaming rules keep O(1) or O(window) state
and update in one pass. Batch rules need the member set to recompute, so they
keep a buffer and pay for it in memory. `memory_per_cluster` makes that cost
visible, because "robust to outliers" is not free.

`RobustTrim` and `Huber` are the serious candidates. `RunningMean` is included as
the baseline it is: an outlier moves a running mean without limit, and each
outlier then attracts the points after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np

#: Every rule returns a unit-norm row. The clustering works in cosine, so a
#: centroid that drifts off the sphere would make distances incomparable.
Unit = np.ndarray


def _unit(vector: np.ndarray) -> Unit:
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float32, copy=False) if norm > 0.0 else vector


@runtime_checkable
class CentroidRule(Protocol):
    """Updates one cluster's centroid as points arrive.

    Implementations are stateful: one instance per cluster, so a rule may keep a
    running sum or a buffer without any shared mutable state.
    """

    #: Short identifier used in result tables and audit reports.
    name: str

    def update(self, centroid: Unit, point: Unit) -> Unit:
        """Return the centroid after `point` joins the cluster."""
        ...

    def points_retained(self) -> int:
        """How many past points this rule keeps. 0 means constant memory."""
        ...


class RunningMean:
    """The plain arithmetic mean of everything assigned. The baseline.

    Included because it is what the other rules have to beat, and because an
    online method that uses it is the obvious first thing anyone would write.
    """

    name = "running_mean"

    def __init__(self) -> None:
        self._sum: Unit | None = None
        self._n = 0

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        # Underscored, not because the parameter is spurious but because the
        # running sum supersedes it after the first point. It is part of the
        # `update` protocol and cannot be dropped.
        self._n += 1
        if self._sum is None:
            self._sum = point.astype(np.float64).copy()
        else:
            self._sum += point
        return _unit(self._sum / self._n)

    def points_retained(self) -> int:
        return 0


class Ema:
    """Exponential moving average: recent points dominate, old ones fade.

    Bounds how far a single point can move the centroid - a runaway outlier
    cannot drag it as far as a running mean will - at the cost of a bias that
    depends on the decay rather than on the data.
    """

    def __init__(self, decay: float = 0.98) -> None:
        if not 0.0 < decay < 1.0:
            raise ValueError(f"decay must be in (0, 1), got {decay}")
        self.name = f"ema_{decay:g}"
        self._decay = decay
        self._initialised = False

    def update(self, centroid: Unit, point: Unit) -> Unit:
        if not self._initialised:
            self._initialised = True
            return _unit(point.copy())
        return _unit(self._decay * centroid + (1.0 - self._decay) * point)

    def points_retained(self) -> int:
        return 0


class SlidingWindow:
    """Mean of the last `window` points. Forgets deliberately.

    Unbounded accumulation is a liability in a system that runs for months, but a
    sliding window throws away the evidence that a cluster was coherent a while
    ago, so it drifts toward whatever the window happens to contain.
    """

    name = "sliding_window"

    def __init__(self, window: int = 32) -> None:
        if window < 2:
            raise ValueError(f"window must be at least 2, got {window}")
        self.name = f"sliding_{window}"
        self._window = window
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        # The current centroid is genuinely redundant: with one buffered point it
        # *is* that point. Underscored because the rule is part of the `update`
        # protocol and cannot drop the parameter.
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        return _unit(np.mean(self._buffer, axis=0))

    def points_retained(self) -> int:
        return self._window


class RobustTrim:
    """Mean of the middle fraction of the buffer, by distance from its own mean.

    The straightforward way to stop an outlier moving a centre: drop the worst
    `trim` fraction each round, then average what is left. It re-estimates from
    the whole buffer rather than only nudging, which is what makes it recover
    *after* contamination rather than merely resisting it.
    """

    name = "robust_trim"

    def __init__(self, window: int = 64, trim: float = 0.2) -> None:
        if window < 3:
            raise ValueError(f"window must be at least 3, got {window}")
        if not 0.0 < trim < 0.5:
            raise ValueError(f"trim must be in (0, 0.5), got {trim}")
        self.name = f"robust_trim_{window}_{trim:g}"
        self._window = window
        self._keep = max(1, round(window * (1.0 - trim)))
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        # As with the sliding window, the re-estimate comes entirely from the
        # buffer, so the current centroid is redundant once the buffer is seeded.
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        centre = np.mean(stacked, axis=0)
        distances = np.linalg.norm(stacked - centre, axis=1)
        # The buffer starts empty and only reaches `_keep` once the cluster has
        # seen enough members, so the kth is clamped rather than trusted: a
        # freshly-spawned cluster has exactly one point and must not raise.
        kth = min(self._keep, len(stacked)) - 1
        # Partition rather than argsort: only the retained rows are needed, and
        # this is O(n) against O(n log n) on every single point update.
        keep = np.argpartition(distances, kth)[: kth + 1]
        return _unit(np.mean(stacked[keep], axis=0))

    def points_retained(self) -> int:
        return self._window


class Huber:
    """Mean with iteratively reweighted points, downweighting large residuals.

    Unlike trimming, the down-weighting is smooth rather than a hard cut, so
    there is no threshold at which a point is abruptly included or discarded. It
    costs a few passes over the buffer per update.
    """

    name = "huber"

    def __init__(self, window: int = 64, delta: float = 1.0, iterations: int = 2) -> None:
        if window < 3:
            raise ValueError(f"window must be at least 3, got {window}")
        if delta <= 0.0:
            raise ValueError(f"delta must be positive, got {delta}")
        self.name = f"huber_{window}_{delta:g}"
        self._window = window
        self._delta = delta
        self._iterations = iterations
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        # Re-estimated from the buffer each time, so the incoming centroid is
        # not consulted; see `RunningMean.update` for why it is underscored.
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        estimate = np.mean(stacked, axis=0)
        for _ in range(self._iterations):
            residuals = np.linalg.norm(stacked - estimate, axis=1)
            # Huber weight: 1 for small residuals, linear decay beyond delta.
            weights = np.where(
                residuals <= self._delta, 1.0, self._delta / np.maximum(residuals, 1e-12)
            )
            total = float(weights.sum())
            if total <= 0.0:
                break
            estimate = (weights[:, None] * stacked).sum(axis=0) / total
        return _unit(estimate)

    def points_retained(self) -> int:
        return self._window


class Medoid:
    """The actual member nearest the centre. Robust, and legible.

    Two reasons this exists. It cannot be dragged by an outlier for the same
    reason trimming cannot - the estimate is one real observation, so the worst
    case is bounded by the data's own spread. And unlike every other rule it
    returns a string a human can read, which is what the cluster label should
    ultimately be seeded from. It is expensive: O(window^2) per update, which is
    the price of the legibility and has to be paid for deliberately.
    """

    name = "medoid"

    def __init__(self, window: int = 32) -> None:
        if window < 2:
            raise ValueError(f"window must be at least 2, got {window}")
        self.name = f"medoid_{window}"
        self._window = window
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        # The medoid is picked from the buffer, so the incoming centroid is not
        # consulted; see `RunningMean.update` for why it is underscored.
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        # Sumed distance to every other member, no square root needed: the
        # ordering is identical and it is the cheaper operation.
        totals = np.linalg.norm(stacked[:, None, :] - stacked[None, :, :], axis=2).sum(axis=1)
        # `np.asarray` is a runtime no-op here; it exists so the row type is
        # pinned for the type checker rather than inferred as `Any`.
        return np.asarray(stacked[int(np.argmin(totals))])

    def points_retained(self) -> int:
        return self._window


def display_member(centroid: Unit, members: Sequence[str], vectors: np.ndarray) -> str:
    """The member text nearest a centroid - the legible stand-in for its name.

    Kept separate from the assignment centroid on purpose. The vector that
    decides assignments and the string a human reads are different jobs, and
    conflating them is how a cluster ends up named after one of its outliers.
    """
    if not members:
        return ""
    scores = np.asarray(vectors, dtype=np.float32) @ centroid
    return members[int(np.argmax(scores))]
