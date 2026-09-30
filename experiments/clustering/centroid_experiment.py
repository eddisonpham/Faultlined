"""Which centroid rule should online clustering use?

Resolved before the factorial, on purpose. In an online method the update rule is
not a detail of the algorithm - it *is* the algorithm. A batch method averages a
fixed set once; an online method lets every arriving point move the centre that
decides where the next point goes, so a weak rule compounds rather than averaging
out. Measured inside a 30-config factorial it would also be unreadable.

Three properties decide it, and they can disagree:

1. **Drift under reordering.** Feed identical data in different orders and
   measure how far the centroids move. Lower is better, and this is the property
   that decides whether a human's confirmed labels survive.
2. **Outlier resistance.** Admit one point that does not belong and measure how
   far the centroid is dragged. An online method sees bad data eventually, and
   the recovery behaviour is what separates a robust rule from a merely
   well-behaved one on clean input.
3. **Cost.** Memory per cluster and time per update. Robustness is not free and
   the engine runs continuously, so a rule that needs a large buffer is a real
   liability even if it wins on the first two.

Accuracy is checked too, but as a guard rather than an objective: a rule that wins
on stability by degrading the clustering has not won.

Everything here runs on dev pairs only. The held-out set is not touched, and the
runner refuses to run this against it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from experiments.clustering import embeddings
from experiments.clustering.centroids import (
    CentroidRule,
    Ema,
    Huber,
    Medoid,
    RobustTrim,
    RunningMean,
    SlidingWindow,
)
from experiments.clustering.evaluation import metrics
from experiments.clustering.methods.online_centroids import OnlineCentroids

#: Candidate rules, in the order a reader should consider them. Each is
#: instantiated afresh per cluster, so they are factories.
RULES: dict[str, Callable[[], CentroidRule]] = {
    "running_mean": RunningMean,
    "ema_0.98": lambda: Ema(0.98),
    "ema_0.90": lambda: Ema(0.90),
    "sliding_32": lambda: SlidingWindow(32),
    "sliding_8": lambda: SlidingWindow(8),
    "robust_trim_64_0.2": lambda: RobustTrim(64, 0.2),
    "robust_trim_32_0.25": lambda: RobustTrim(32, 0.25),
    "huber_64_1.0": lambda: Huber(64, 1.0),
    "medoid_32": lambda: Medoid(32),
}


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """Everything measured about one rule, on one input."""

    rule: str
    #: Mean cosine displacement of centroids across input orderings, / points.
    #: Zero would mean the answer is completely order-independent.
    drift: float
    #: Cosine displacement caused by a single admitted outlier. Zero would mean
    #: the rule ignores contamination entirely.
    outlier_pull: float
    #: Fraction of the original centroid recovered after the outlier is removed
    #: and the remaining points are replayed. 1.0 is full recovery.
    recovery: float
    #: Wall-clock microseconds per accepted point, averaged over the runs.
    micros_per_point: float
    points_retained: int
    cluster_count: int
    #: Assigned pairs, so a rule cannot buy stability by collapsing everything.
    pair_f1: float
    over_merge_rate: float
    under_merge_rate: float
    #: How many *different* cluster counts the rule produced across input orders.
    #: Above 1 means the method's own answer to "how many tasks are there" is
    #: order-dependent, which no amount of accuracy forgives.
    distinct_counts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "drift": self.drift,
            "outlier_pull": self.outlier_pull,
            "recovery": self.recovery,
            "micros_per_point": self.micros_per_point,
            "points_retained": self.points_retained,
            "cluster_count": self.cluster_count,
            "distinct_counts": self.distinct_counts,
            "pair_f1": self.pair_f1,
            "over_merge_rate": self.over_merge_rate,
            "under_merge_rate": self.under_merge_rate,
        }


def _orderings(count: int, seed: int = 1729) -> list[np.ndarray]:
    """Deterministic permutations. A fixed seed keeps the run reproducible."""
    rng = np.random.default_rng(seed)
    return [rng.permutation(count) for _ in range(count)]


def _centroids(vectors: np.ndarray, order: np.ndarray, rule: str, radius: float) -> np.ndarray:
    model = OnlineCentroids(radius=radius, rule_factory=RULES[rule])
    model.fit(vectors[order], [str(i) for i in order])
    return np.stack([c.centroid for c in model.clusters])


def drift_across_orders(vectors: np.ndarray, rule: str, radius: float) -> tuple[float, int]:
    """Mean centroid displacement across permutations of the same data.

    The reference is the centroid set from the identity order. Cluster counts are
    compared too, because two runs that disagree about *how many* clusters there
    are are not comparable at all, and averaging their centroids would be
    meaningless - so a run with a different count is skipped rather than coerced.
    """
    orders = _orderings(len(vectors))
    reference = _centroids(vectors, np.arange(len(vectors)), rule, radius)
    distances: list[float] = []
    counts: set[int] = {len(reference)}
    for order in orders:
        current = _centroids(vectors, order, rule, radius)
        counts.add(len(current))
        if len(current) != len(reference):
            continue
        # Best-match each reference centroid to a current one before comparing,
        # so a legitimate relabelling of clusters is not counted as movement.
        similarity = reference @ current.T
        matched = float(np.mean(1.0 - similarity.max(axis=1)))
        distances.append(matched)
    return (float(np.mean(distances)) if distances else 0.0), len(counts)


def outlier_pull(vectors: np.ndarray, rule: str, radius: float) -> tuple[float, float]:
    """How far one bad point drags a centroid, and how much of that comes back.

    A point is placed opposite the data on the unit sphere - maximally wrong, not
    plausibly wrong - so the measurement is a worst case rather than a typical
    one. After the pull, the outlier is dropped and the rest replayed to see
    whether the centroid returns on its own.
    """
    identity = np.arange(len(vectors))
    clean = _centroids(vectors, identity, rule, radius)
    baseline = _orderings(len(vectors))[0]
    replay_order = np.concatenate([baseline, [len(vectors)]])
    contaminated = vectors.copy()
    outlier = -vectors[baseline[0]].astype(np.float32)
    contaminated = np.vstack([contaminated, outlier[None, :]])

    with_outlier = _centroids(contaminated, replay_order, rule, radius)
    if len(with_outlier) != len(clean):
        # A new cluster absorbed the outlier rather than moving an old one, which
        # is the best possible outcome and shows up as zero pull.
        return 0.0, 1.0
    similarity = clean @ with_outlier.T
    pull = float(np.mean(1.0 - similarity.max(axis=1)))

    recovered = _centroids(vectors, baseline, rule, radius)
    similarity_back = clean @ recovered.T
    recovery = float(np.mean(similarity_back.max(axis=1)))
    return pull, recovery


def _timing(vectors: np.ndarray, rule: str, radius: float, repeats: int = 3) -> float:
    order = np.arange(len(vectors))
    total = 0.0
    for _ in range(repeats):
        model = OnlineCentroids(radius=radius, rule_factory=RULES[rule])
        start = time.perf_counter()
        model.fit(vectors[order], [str(i) for i in order])
        total += time.perf_counter() - start
    return (total / repeats / max(1, len(vectors))) * 1e6


def run_rule(
    rule: str,
    encoder: embeddings.Encoder,
    texts: Sequence[str],
    *,
    radius: float,
    scorer: Callable[[Sequence[int], Sequence[str], str], metrics.PairScores],
) -> RuleOutcome:
    """Measure one rule end to end on the given input."""
    vectors = embeddings.encode_all(encoder, texts)
    drift, distinct_counts = drift_across_orders(vectors, rule, radius)
    pull, recovery = outlier_pull(vectors, rule, radius)
    micros = _timing(vectors, rule, radius)

    model = OnlineCentroids(radius=radius, rule_factory=RULES[rule])
    result = model.fit(vectors, texts)
    scores = scorer(result.labels, texts, rule)
    return RuleOutcome(
        rule=rule,
        drift=drift,
        outlier_pull=pull,
        recovery=recovery,
        micros_per_point=micros,
        points_retained=RULES[rule]().points_retained(),
        cluster_count=result.cluster_count,
        distinct_counts=distinct_counts,
        pair_f1=scores.pair_f1,
        over_merge_rate=scores.over_merge_rate,
        under_merge_rate=scores.under_merge_rate,
    )
