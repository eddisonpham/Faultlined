"""Online constrained-centroid clustering: the method proposed in the plan.

Leader-follower with refinement, plus the two constraints that make it usable in
production rather than merely demonstrable.

**Leader-follower.** A new embedding joins the nearest cluster if it is within
`radius` (in cosine distance, so 0 is identical and 2 is opposite); otherwise it
becomes a new cluster. So the number of clusters is discovered from the data
rather than fixed in advance, which is the property k-means cannot offer and the
reason a fixed-`k` method is the wrong shape for this problem.

**Refinement.** A brand-new cluster's centroid is a single point, which is a noisy
estimate. As members accumulate the centroid moves toward the mean of what it has
actually attracted. This is where the centroid rule from `centroids.py` enters:
it is the only part that decides how the centre moves.

**Freeze.** A cluster a human has confirmed does not move. This is the single
most important property here, and the reason a plain leader-follower is not
enough: without it, ingesting a batch of new episodes renames the clusters someone
curated an hour ago, and every label they confirmed becomes wrong. A confirmed
cluster is a promise, and freezing is what makes it one.

A consequence worth stating: a frozen cluster cannot absorb a genuinely different
task, so the assignment is a cost, not a decision. A point that sits within
`radius` of a frozen cluster is still assigned there, because a human said so.
The escape hatch is `split`, not silent drift.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from experiments.clustering.centroids import CentroidRule, RunningMean


@dataclass(slots=True)
class Cluster:
    """One cluster's mutable state. A rule instance per cluster, not per run."""

    centroid: np.ndarray
    rule: CentroidRule
    count: int = 1
    label: str = ""
    #: Confirmed clusters are frozen: assignments still land in them, but the
    #: centroid is never updated again.
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
    #: Mean cosine distance from each member to its own final centroid. A
    #: cohesion read-out that does not need the gold labels.
    cohesion: float
    #: Largest cluster by member count, as a fraction. Near 1.0 means the method
    #: collapsed everything into one cluster, which is a common silent failure.
    dominance: float
    #: Centroid moves per accepted assignment, total. The cost the centroid
    #: experiment measures directly.
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
        # At the cap: the nearest cluster absorbs the point even though it is
        # outside the radius. Dropping it would lose data; opening a new cluster
        # would break the cap the operator set.
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
        # Nearest centroid wins, matching how a point was assigned on the way in.
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
