"""Online centroid clustering over extracted cores, in pure Python."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

DIM = 256


def token_vector(text: str, *, dim: int = DIM) -> list[float]:
    """Hashed bag of tokens, L2-normalised."""
    vector = [0.0] * dim
    for token in text.split():
        digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
        index = int.from_bytes(digest, "big") % dim
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity."""
    if not left or not right:
        return 0.0
    total = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return total / (left_norm * right_norm)


Rule = Callable[[], "CentroidRule"]


class CentroidRule:
    """How a cluster's centroid moves as members arrive."""

    def update(self, _centroid: Sequence[float], _point: Sequence[float]) -> list[float]:
        raise NotImplementedError

    def describe(self) -> str:
        return type(self).__name__.lower()


@dataclass(slots=True)
class RunningMean(CentroidRule):
    """Arithmetic mean of everything assigned."""

    count: int = 0

    def update(self, centroid: Sequence[float], point: Sequence[float]) -> list[float]:
        self.count += 1
        total = self.count
        return [
            (existing * (total - 1) + value) / total
            for existing, value in zip(centroid, point, strict=True)
        ]


@dataclass(slots=True)
class SlidingWindow(CentroidRule):
    """Mean of the last `window` points."""

    window: int = 8
    buffer: list[list[float]] = field(default_factory=list)

    def update(self, _centroid: Sequence[float], point: Sequence[float]) -> list[float]:
        self.buffer.append(list(point))
        if len(self.buffer) > self.window:
            self.buffer.pop(0)
        count = len(self.buffer)
        return [sum(values) / count for values in zip(*self.buffer, strict=True)]


@dataclass(slots=True)
class Ema(CentroidRule):
    """Exponential moving average: recent points dominate, old ones fade."""

    alpha: float = 0.98

    def update(self, centroid: Sequence[float], point: Sequence[float]) -> list[float]:
        return [
            self.alpha * existing + (1.0 - self.alpha) * value
            for existing, value in zip(centroid, point, strict=True)
        ]


RULES: dict[str, Rule] = {
    "running_mean": RunningMean,
    "sliding_8": lambda: SlidingWindow(8),
    "sliding_32": lambda: SlidingWindow(32),
    "ema_0.98": lambda: Ema(0.98),
}


@dataclass(slots=True)
class Cluster:
    """One cluster's state."""

    centroid: list[float]
    rule: CentroidRule
    label: str = ""
    frozen: bool = False
    members: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    def absorb(self, point: Sequence[float], member: str) -> None:
        """Add a member."""
        self.members.append(member)
        if not self.frozen:
            self.centroid = self.rule.update(self.centroid, point)


@dataclass(frozen=True, slots=True)
class Assignment:
    """What happened to one core on the way in."""

    core: str
    cluster: int
    spawned: bool
    into_frozen: bool
    distance: float


class OnlineCentroids:
    """Streaming clustering with a discoverable cluster count and frozen labels."""

    name = "online_centroids"

    def __init__(
        self,
        *,
        radius: float = 0.30,
        max_clusters: int = 512,
        rule: str = "running_mean",
    ) -> None:
        if not 0.0 <= radius <= 2.0:
            raise ValueError(f"radius must be in [0, 2], got {radius}")
        if max_clusters < 1:
            raise ValueError(f"max_clusters must be at least 1, got {max_clusters}")
        if rule not in RULES:
            raise ValueError(f"unknown centroid rule {rule!r}; known: {sorted(RULES)}")
        self.radius = radius
        self.max_clusters = max_clusters
        self.rule_name = rule
        self.clusters: list[Cluster] = []
        self.assignments: list[Assignment] = []

    def distance_to(self, cluster: Cluster, point: Sequence[float]) -> float:
        return 1.0 - cosine(cluster.centroid, point)

    def observe(self, core: str, vector: Sequence[float] | None = None) -> Assignment:
        """Admit one core, and report what happened to it."""
        point = list(vector) if vector is not None else token_vector(core)
        nearest: int | None = None
        best = float("inf")
        for index, cluster in enumerate(self.clusters):
            distance = self.distance_to(cluster, point)
            if distance < best:
                best = distance
                nearest = index

        if nearest is not None and best <= self.radius:
            cluster = self.clusters[nearest]
            into_frozen = cluster.frozen
            cluster.absorb(point, core)
            assignment = Assignment(core, nearest, False, into_frozen, best)
        elif len(self.clusters) < self.max_clusters:
            self.clusters.append(
                Cluster(centroid=list(point), rule=RULES[self.rule_name](), members=[core])
            )
            assignment = Assignment(core, len(self.clusters) - 1, True, False, 0.0)
        else:
            cluster = self.clusters[nearest] if nearest is not None else self.clusters[0]
            cluster.absorb(point, core)
            assignment = Assignment(core, self.clusters.index(cluster), False, cluster.frozen, best)
        self.assignments.append(assignment)
        return assignment

    def observe_all(self, cores: Iterable[str]) -> list[Assignment]:
        return [self.observe(core) for core in cores]

    def confirm(self, index: int, label: str) -> None:
        """Freeze a cluster and give it a name."""
        if not 0 <= index < len(self.clusters):
            raise IndexError(f"no cluster {index}")
        self.clusters[index].label = label
        self.clusters[index].frozen = True

    def by_core(self) -> dict[str, int]:
        """Every core seen, and the cluster it ended up in."""
        return {assignment.core: assignment.cluster for assignment in self.assignments}

    @property
    def singletons(self) -> int:
        return sum(1 for cluster in self.clusters if cluster.size == 1)

    @property
    def dominance(self) -> float:
        total = sum(cluster.size for cluster in self.clusters)
        if not total:
            return 0.0
        return max((cluster.size for cluster in self.clusters), default=0) / total

    def centroid_of(self, index: int) -> list[float]:
        return list(self.clusters[index].centroid)
