"""Per-scope control limits: EWMA centre plus a median/MAD robust spread."""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

DEFAULT_Z_THRESHOLD = 3.5

MIN_OBSERVATIONS = 20
SAMPLE_WINDOW = 50
EWMA_ALPHA = 0.2
MAX_HOLD_SECONDS = 3600.0

RELATIVE_SIGMA_FLOOR = 0.05
ABSOLUTE_SIGMA_FLOOR = 1e-9

BaselineKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Baseline:
    """One (feature, scope) control limit."""

    feature: str
    scope: str = ""
    samples: tuple[float, ...] = ()
    center: float = 0.0
    breached: bool = False
    held_since: float | None = None

    @property
    def observations(self) -> int:
        return len(self.samples)

    @property
    def median(self) -> float:
        if not self.samples:
            return self.center
        return statistics.median(self.samples)

    @property
    def mad(self) -> float:
        """Median absolute deviation; 0 for fewer than two samples."""
        if len(self.samples) < 2:
            return 0.0
        centre = self.median
        return statistics.median([abs(value - centre) for value in self.samples])

    @property
    def sigma(self) -> float:
        """MAD converted to a sigma-equivalent, with a scale-aware floor."""
        centre = self.median
        floor = max(RELATIVE_SIGMA_FLOOR * abs(centre), ABSOLUTE_SIGMA_FLOOR)
        return max(1.4826 * self.mad, floor)

    def warm(self, minimum: int = MIN_OBSERVATIONS) -> bool:
        return self.observations >= minimum

    def robust_z(self, value: float) -> float:
        """Deviation in robust sigmas; 0.0 while cold, so callers must check ``warm``."""
        if not self.warm():
            return 0.0
        return (value - self.median) / self.sigma

    def ratio_to_center(self, value: float) -> float:
        """Value relative to the EWMA centre, guarded against a zero centre."""
        if self.center == 0.0:
            return float("inf") if value > 0 else 1.0
        return value / self.center

    def to_row(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "scope": self.scope,
            "samples": list(self.samples),
            "center": self.center,
            "breached": self.breached,
            "held_since": self.held_since,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Baseline:
        samples = tuple(float(value) for value in (row.get("samples") or ()))
        return cls(
            feature=str(row["feature"]),
            scope=str(row.get("scope") or ""),
            samples=samples[-SAMPLE_WINDOW:],
            center=float(row.get("center") or 0.0),
            breached=bool(row.get("breached")),
            held_since=(None if row.get("held_since") is None else float(row["held_since"])),
        )


class BaselineBook:
    """Mutable set of control limits, keyed by ``(feature, scope)``."""

    def __init__(self, baselines: Mapping[BaselineKey, Baseline] | None = None) -> None:
        self._baselines: dict[BaselineKey, Baseline] = dict(baselines or {})

    def __contains__(self, key: BaselineKey) -> bool:
        return key in self._baselines

    def __iter__(self) -> Iterator[BaselineKey]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self._baselines)

    def keys(self) -> list[BaselineKey]:
        return sorted(self._baselines)

    def get(self, feature: str, scope: str = "") -> Baseline | None:
        return self._baselines.get((feature, scope))

    def values(self) -> list[Baseline]:
        return [self._baselines[key] for key in self.keys()]

    def observe(self, feature: str, value: float, *, scope: str = "") -> Baseline:
        """Record one observation, unless the scope is currently held."""
        key: BaselineKey = (feature, scope)
        current = self._baselines.get(key) or Baseline(feature=feature, scope=scope)
        if current.breached:
            return current
        window = (*current.samples, float(value))[-SAMPLE_WINDOW:]
        center = (
            float(value)
            if not current.samples
            else current.center + EWMA_ALPHA * (float(value) - current.center)
        )
        updated = replace(current, samples=window, center=center)
        self._baselines[key] = updated
        return updated

    def observe_many(self, items: Iterable[tuple[str, float, str]]) -> None:
        for feature, value, scope in items:
            self.observe(feature, value, scope=scope)

    def hold(self, feature: str, *, scope: str = "", now: float = 0.0) -> Baseline:
        """Freeze a scope that is currently in breach."""
        key: BaselineKey = (feature, scope)
        current = self._baselines.get(key) or Baseline(feature=feature, scope=scope)
        if not current.breached:
            current = replace(current, breached=True, held_since=now)
        self._baselines[key] = current
        return current

    def release(self, feature: str, *, scope: str = "") -> Baseline:
        """Resume absorbing after the breach clears."""
        key: BaselineKey = (feature, scope)
        current = self._baselines.get(key)
        if current is None:
            return Baseline(feature=feature, scope=scope)
        updated = replace(current, breached=False, held_since=None)
        self._baselines[key] = updated
        return updated

    def release_expired(
        self, *, now: float, max_hold_seconds: float = MAX_HOLD_SECONDS
    ) -> list[BaselineKey]:
        """Drop holds older than ``max_hold_seconds``."""
        expired: list[BaselineKey] = []
        for key in self.keys():
            baseline = self._baselines[key]
            if not baseline.breached or baseline.held_since is None:
                continue
            if now - baseline.held_since >= max_hold_seconds:
                self._baselines[key] = replace(baseline, breached=False, held_since=None)
                expired.append(key)
        return expired

    def warm_scopes(self) -> list[BaselineKey]:
        """Keys with enough observations for a statistical rule to use them."""
        return [key for key in self.keys() if self._baselines[key].warm()]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> BaselineBook:
        return cls({(b.feature, b.scope): b for b in (Baseline.from_row(r) for r in rows)})
