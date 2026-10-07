"""Online constrained-centroid clustering: the method proposed in the plan."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from experiments.clustering.centroids import CentroidRule, RunningMean


@dataclass(slots=True)
class Cluster:
    """One cluster's mutable state."""

    centroid: np.ndarray
    rule: CentroidRule
    count: int = 1
    label: str = ""
    frozen: bool = False
    members: list[str] = field(default_factory=list)

    def absorb(self, point: np.ndarray, text: str) -> None:
        self.count += 1
        self.members.append(text)
        if not self.frozen:
            self.centroid = self.rule.update(self.centroid, point)


@dataclass(frozen=True, slots=True)
class OnlineResult:
    """Labels after a pass over the data, plus the cost of getting there."""

    labels: tuple[int, ...]
    cluster_count: int
    cohesion: float
    dominance: float
    centroid_updates: int

    def label_of(self, index: int) -> int:
        return self.labels[index]


class OnlineCentroids:
    """Streaming clustering with a discoverable cluster count and frozen labels."""

    name = "online_centroids"

    def __init__(
        self,
        *,
        radius: float = 0.35,
        max_clusters: int = 64,
        rule_factory: Callable[[], CentroidRule] = RunningMean,
    ) -> None:
        if not 0.0 < radius < 2.0:
            raise ValueError(f"radius must be in (0, 2), got {radius}")
        if max_clusters < 1:
            raise ValueError(f"max_clusters must be at least 1, got {max_clusters}")
        self.radius = radius
        self.max_clusters = max_clusters
        self._rule_factory = rule_factory
        self._clusters: list[Cluster] = []
        self._updates = 0

    @property
    def clusters(self) -> list[Cluster]:
        return self._clusters

    def distances(self, point: np.ndarray) -> np.ndarray:
        """Cosine distance from a unit point to every cluster centroid."""
        if not self._clusters:
            return np.zeros(0, dtype=np.float32)
        stacked = np.stack([c.centroid for c in self._clusters])
        return np.asarray(1.0 - stacked @ point, dtype=np.float32)

    def fit(self, vectors: np.ndarray, texts: Sequence[str]) -> OnlineResult:
        """Consume the data in order, one point at a time."""
        for row, text in zip(vectors, texts, strict=True):
            self.observe(np.asarray(row, dtype=np.float32), text)
        return self.result(vectors)

    def observe(self, point: np.ndarray, text: str) -> None:
        distances = self.distances(point)
        if distances.size == 0:
            self._spawn(point, text)
            return
        nearest = int(np.argmin(distances))
        if float(distances[nearest]) <= self.radius:
            cluster = self._clusters[nearest]
            was_frozen = cluster.frozen
            cluster.absorb(point, text)
            if not was_frozen:
                self._updates += 1
            return
        if len(self._clusters) < self.max_clusters:
            self._spawn(point, text)
            return
        self._clusters[nearest].absorb(point, text)
        self._updates += 1

    def _spawn(self, point: np.ndarray, text: str) -> None:
        self._clusters.append(
            Cluster(centroid=point.copy(), rule=self._rule_factory(), members=[text])
        )

    def confirm(self, index: int, label: str) -> None:
        """Freeze a cluster and give it a name."""
        cluster = self._clusters[index]
        cluster.label = label
        cluster.frozen = True

    def result(self, vectors: np.ndarray) -> OnlineResult:
        if not self._clusters:
            return OnlineResult((), 0, 0.0, 0.0, 0)
        stacked = np.stack([c.centroid for c in self._clusters])
        labels = tuple(int(i) for i in np.argmax(vectors @ stacked.T, axis=1))
        sizes = np.array([c.count for c in self._clusters], dtype=np.float64)
        total = float(sizes.sum()) or 1.0
        chosen = stacked[np.array(labels, dtype=int)]
        cohesion = float(np.mean(1.0 - np.einsum("ij,ij->i", vectors, chosen)))
        return OnlineResult(
            labels=labels,
            cluster_count=len(self._clusters),
            cohesion=cohesion,
            dominance=float(sizes.max() / total),
            centroid_updates=self._updates,
        )
