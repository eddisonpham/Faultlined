"""Metrics, ordered by how much they actually answer the question.

The gold set is a set of *pair* labels, so the primary metrics are pair-level.
That is a deliberate choice. ARI and B-cubed compare partitions and are the
standard numbers, but they average over every item and will happily report a
healthy score for a clustering that merged "place the bowl on the plate" with
"screw in the bolt". This domain has one failure that matters more than the rest
- two different things landing in one cluster - and only a pair metric names it
directly.

`over_merge_rate` is therefore the headline. A method that is wrong in that
direction produces a dataset build that silently mixes tasks, and no aggregate
partition score makes that obvious. `under_merge_rate` is its opposite and much
cheaper to live with: a task split across two clusters shows up as two smaller
clusters, and a human merges them.

Ambiguous pairs are never scored. They are counted and returned separately,
because folding them into either class would let a method score well by accident
on calls a human could argue either way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from experiments.clustering.evaluation.gold import GoldPair


@dataclass(frozen=True, slots=True)
class PairScores:
    """Agreement between a clustering and the gold pair labels for one view."""

    over_merge_rate: float
    under_merge_rate: float
    pair_precision: float
    pair_recall: float
    pair_f1: float
    scored_pairs: int
    ambiguous_pairs: int
    #: Gold "different" pairs that were merged, with the texts. The audit reads
    #: this; a number alone does not say what went wrong.
    over_merged: tuple[tuple[str, str], ...] = ()
    #: Gold "same" pairs that were split apart.
    under_merged: tuple[tuple[str, str], ...] = ()
    #: Strings the clustering never assigned. Reported so a method that quietly
    #: drops inputs cannot read as a clean result.
    unassigned: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, float | int]:
        return {
            "over_merge_rate": self.over_merge_rate,
            "under_merge_rate": self.under_merge_rate,
            "pair_precision": self.pair_precision,
            "pair_recall": self.pair_recall,
            "pair_f1": self.pair_f1,
            "scored_pairs": self.scored_pairs,
            "ambiguous_pairs": self.ambiguous_pairs,
            "unassigned": len(self.unassigned),
        }


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def pair_scores(
    labels: Mapping[str, int],
    pairs: Iterable[GoldPair],
    view: str,
) -> PairScores:
    """Score a clustering against the decided pairs for one view.

    `labels` maps a task string to its cluster id. A pair is "merged" when both
    strings carry the same label, and "split" otherwise. Unlabelled strings are
    counted as unassigned and reported, because a method that silently drops
    inputs would otherwise look clean.
    """
    attribute = f"same_{view}"
    positives: list[tuple[str, str]] = []
    negatives: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []

    for pair in pairs:
        verdict = getattr(pair, attribute)
        if verdict is None:
            ambiguous.append((pair.a, pair.b))
        elif verdict:
            positives.append((pair.a, pair.b))
        else:
            negatives.append((pair.a, pair.b))

    def merged(candidate: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(a, b) for a, b in candidate if labels.get(a) == labels.get(b)]

    true_merges = set(merged(positives))
    false_merges = merged(negatives)
    unassigned = tuple(
        sorted({text for pair in pairs for text in (pair.a, pair.b) if text not in labels})
    )

    recall = _rate(len(true_merges), len(positives))
    precision = _rate(len(true_merges), len(true_merges) + len(false_merges))
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return PairScores(
        over_merge_rate=_rate(len(false_merges), len(negatives)),
        under_merge_rate=_rate(len(positives) - len(true_merges), len(positives)),
        pair_precision=precision,
        pair_recall=recall,
        pair_f1=f1,
        scored_pairs=len(positives) + len(negatives),
        ambiguous_pairs=len(ambiguous),
        over_merged=tuple(false_merges),
        under_merged=tuple(pair for pair in positives if pair not in true_merges),
        unassigned=unassigned,
    )


def _comb2(n: int) -> float:
    return n * (n - 1) / 2.0


def adjusted_rand(left: Sequence[int], right: Sequence[int]) -> float:
    """Adjusted Rand Index between two labellings of the same items.

    Returns 0.0 when both labellings are degenerate (everything in one cluster),
    where the index is 0/0 and any value would be an invention.
    """
    if len(left) != len(right):
        raise ValueError("label sequences must be the same length")
    n = len(left)
    if n < 2:
        return 0.0
    contingency: dict[tuple[int, int], int] = {}
    a_counts: dict[int, int] = {}
    b_counts: dict[int, int] = {}
    for a, b in zip(left, right, strict=True):
        contingency[(a, b)] = contingency.get((a, b), 0) + 1
        a_counts[a] = a_counts.get(a, 0) + 1
        b_counts[b] = b_counts.get(b, 0) + 1

    index = sum(_comb2(v) for v in contingency.values())
    sum_a = sum(_comb2(v) for v in a_counts.values())
    sum_b = sum(_comb2(v) for v in b_counts.values())
    total = _comb2(n)
    if total == 0.0:
        return 0.0
    expected = sum_a * sum_b / total
    denominator = 0.5 * (sum_a + sum_b) - expected
    if denominator == 0.0:
        return 0.0
    return (index - expected) / denominator


def b_cubed(labels: Sequence[int], gold: Sequence[int]) -> float:
    """Per-item F1 between a predicted and a gold partition."""
    if len(labels) != len(gold):
        raise ValueError("label sequences must be the same length")
    predicted_sizes: dict[int, int] = {}
    gold_sizes: dict[int, int] = {}
    for a, b in zip(labels, gold, strict=True):
        predicted_sizes[a] = predicted_sizes.get(a, 0) + 1
        gold_sizes[b] = gold_sizes.get(b, 0) + 1

    joint: dict[tuple[int, int], int] = {}
    for a, b in zip(labels, gold, strict=True):
        joint[(a, b)] = joint.get((a, b), 0) + 1

    total = 0.0
    for index, (a, b) in enumerate(zip(labels, gold, strict=True)):
        both = joint[(a, b)]
        # A singleton in both labellings agrees perfectly; the formula gives 0/0.
        if predicted_sizes[a] == 1 and gold_sizes[b] == 1:
            total += 1.0
            continue
        precision = both / predicted_sizes[a]
        recall = both / gold_sizes[b]
        total += 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        del index
    return total / len(labels) if labels else 0.0


@dataclass(frozen=True, slots=True)
class Stability:
    """How much a method's output moves when the input order moves.

    This is the metric that decides whether the method is usable online. A
    clustering that reshuffles with input order means every label a human
    confirmed becomes wrong the next time a batch arrives, whatever its accuracy.
    """

    #: Mean pairwise ARI between runs over different input orders, in [0, 1].
    mean_agreement: float
    #: Worst pairwise ARI. A single bad ordering is enough to ruin a day of
    #: curation, so the worst case is reported next to the mean.
    worst_agreement: float
    #: Cluster counts seen across orders; more than one value means the method's
    #: own answer to "how many tasks are there?" is order-dependent.
    distinct_cluster_counts: int
    orders: int = 0
    extras: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "mean_agreement": self.mean_agreement,
            "worst_agreement": self.worst_agreement,
            "distinct_cluster_counts": self.distinct_cluster_counts,
            "orders": self.orders,
            **self.extras,
        }
