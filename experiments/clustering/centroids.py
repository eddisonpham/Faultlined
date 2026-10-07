"""How a centroid should move when a point joins its cluster."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np

Unit = np.ndarray


def _unit(vector: np.ndarray) -> Unit:
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float32, copy=False) if norm > 0.0 else vector


@runtime_checkable
class CentroidRule(Protocol):
    """Updates one cluster's centroid as points arrive."""

    name: str

    def update(self, centroid: Unit, point: Unit) -> Unit:
        """Return the centroid after `point` joins the cluster."""
        ...

    def points_retained(self) -> int:
        """How many past points this rule keeps. 0 means constant memory."""
        ...


class RunningMean:
    """The plain arithmetic mean of everything assigned."""

    name = "running_mean"

    def __init__(self) -> None:
        self._sum: Unit | None = None
        self._n = 0

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        self._n += 1
        if self._sum is None:
            self._sum = point.astype(np.float64).copy()
        else:
            self._sum += point
        return _unit(self._sum / self._n)

    def points_retained(self) -> int:
        return 0


class Ema:
    """Exponential moving average: recent points dominate, old ones fade."""

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
    """Mean of the last `window` points."""

    name = "sliding_window"

    def __init__(self, window: int = 32) -> None:
        if window < 2:
            raise ValueError(f"window must be at least 2, got {window}")
        self.name = f"sliding_{window}"
        self._window = window
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        return _unit(np.mean(self._buffer, axis=0))

    def points_retained(self) -> int:
        return self._window


class RobustTrim:
    """Mean of the middle fraction of the buffer, by distance from its own mean."""

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
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        centre = np.mean(stacked, axis=0)
        distances = np.linalg.norm(stacked - centre, axis=1)
        kth = min(self._keep, len(stacked)) - 1
        keep = np.argpartition(distances, kth)[: kth + 1]
        return _unit(np.mean(stacked[keep], axis=0))

    def points_retained(self) -> int:
        return self._window


class Huber:
    """Mean with iteratively reweighted points, downweighting large residuals."""

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
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        estimate = np.mean(stacked, axis=0)
        for _ in range(self._iterations):
            residuals = np.linalg.norm(stacked - estimate, axis=1)
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
    """The actual member nearest the centre."""

    name = "medoid"

    def __init__(self, window: int = 32) -> None:
        if window < 2:
            raise ValueError(f"window must be at least 2, got {window}")
        self.name = f"medoid_{window}"
        self._window = window
        self._buffer: list[Unit] = []

    def update(self, _centroid: Unit, point: Unit) -> Unit:
        self._buffer.append(point)
        if len(self._buffer) > self._window:
            del self._buffer[: len(self._buffer) - self._window]
        stacked = np.array(self._buffer, dtype=np.float32)
        totals = np.linalg.norm(stacked[:, None, :] - stacked[None, :, :], axis=2).sum(axis=1)
        return np.asarray(stacked[int(np.argmin(totals))])

    def points_retained(self) -> int:
        return self._window


def display_member(centroid: Unit, members: Sequence[str], vectors: np.ndarray) -> str:
    """The member text nearest a centroid - the legible stand-in for its name."""
    if not members:
        return ""
    scores = np.asarray(vectors, dtype=np.float32) @ centroid
    return members[int(np.argmax(scores))]
