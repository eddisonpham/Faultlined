"""Can the embeddings separate the two classes at all?

This is the premise test for the whole stage. Every clustering method downstream
assumes that "same task" pairs sit closer together than "different task" pairs, by
enough that some distance threshold can tell them apart. If the two similarity
distributions overlap - which they will, because "pick up the red cube" and
"pick up the blue cube" differ by one word and "grab the red cube" and
"pick up the red cube" are the same task written twice - then no threshold works,
and the honest conclusion is that a single-view clustering cannot do this job at
any radius.

The number that answers it is the **overlap**: how far the best achievable
threshold still mixes the classes. A mean similarity for each class is not
enough, because two distributions can share a mean and be perfectly separable.
So this reports the full picture - both distributions, the best threshold, and
what that threshold still gets wrong - rather than a single score that would look
fine either way.

It also reports the same measurement per view, because that is the whole reason
for the two-view design: the action view may be separable while the object view
is not, or the reverse, and that asymmetry is the finding that decides whether
clustering on embeddings is viable at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from experiments.clustering.evaluation.gold import GoldPair, vocab


@dataclass(frozen=True, slots=True)
class ViewSeparability:
    """Where the two classes sit, and the best a single threshold can do."""

    view: str
    positives: int
    negatives: int
    positive_min: float
    positive_mean: float
    negative_max: float
    negative_mean: float
    #: Cosine similarity at the threshold that maximises balanced accuracy. Below
    #: this is "same", above is "different".
    best_threshold: float
    best_accuracy: float
    #: How many negatives fall above the best threshold, and positives below it.
    #: Non-zero on both sides means the classes genuinely interleave.
    false_merges: int
    false_splits: int
    #: The classes are disjoint below this similarity: no false merges at all.
    #: `None` when they interleave at every threshold.
    clean_threshold: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "positives": self.positives,
            "negatives": self.negatives,
            "positive_min": self.positive_min,
            "positive_mean": self.positive_mean,
            "negative_max": self.negative_max,
            "negative_mean": self.negative_mean,
            "best_threshold": self.best_threshold,
            "best_accuracy": self.best_accuracy,
            "false_merges": self.false_merges,
            "false_splits": self.false_splits,
            "clean_threshold": self.clean_threshold,
        }


def _similarity(vectors: dict[str, np.ndarray], a: str, b: str) -> float:
    return float(vectors[a] @ vectors[b])


def _separability_for(
    view: str,
    pairs: Sequence[GoldPair],
    vectors: dict[str, np.ndarray],
) -> ViewSeparability:
    attribute = f"same_{view}"
    positives = [_similarity(vectors, p.a, p.b) for p in pairs if getattr(p, attribute) is True]
    negatives = [_similarity(vectors, p.a, p.b) for p in pairs if getattr(p, attribute) is False]
    if not positives or not negatives:
        raise ValueError(f"view {view!r} has no decided pairs of both classes")

    pos = np.array(sorted(positives))
    neg = np.array(sorted(negatives))

    # Sweep every midpoint between the classes; balanced accuracy, because the
    # two classes are not the same size and plain accuracy would reward a
    # threshold that simply predicts the majority.
    best_threshold = 0.0
    best_accuracy = -1.0
    for candidate in np.concatenate([pos, neg]):
        true_positive = float((pos >= candidate).sum())
        true_negative = float((neg < candidate).sum())
        balanced = 0.5 * (true_positive / len(pos) + true_negative / len(neg))
        if balanced > best_accuracy:
            best_accuracy = balanced
            best_threshold = float(candidate)

    false_merges = int((neg >= best_threshold).sum())
    false_splits = int((pos < best_threshold).sum())

    # A threshold that admits no negative is only possible when the classes do
    # not overlap at all, which is rare and worth distinguishing from "very good".
    clean = float(pos.min()) if float(neg.max()) < float(pos.min()) else None

    return ViewSeparability(
        view=view,
        positives=len(pos),
        negatives=len(neg),
        positive_min=float(pos.min()),
        positive_mean=float(pos.mean()),
        negative_max=float(neg.max()),
        negative_mean=float(neg.mean()),
        best_threshold=best_threshold,
        best_accuracy=best_accuracy,
        false_merges=false_merges,
        false_splits=false_splits,
        clean_threshold=clean,
    )


def separability(
    vectors_by_text: dict[str, np.ndarray],
    pairs: Sequence[GoldPair] | None = None,
) -> dict[str, ViewSeparability]:
    """Measure per-view separability. Uses the full gold set, both splits.

    This is a premise check rather than a scored result: it reports where the two
    classes sit, so it is informative on every pair and does not need a split.
    Deciding a threshold *from* it is a different act, and that is what the dev
    split is for.
    """
    from experiments.clustering.evaluation import gold

    chosen = tuple(pairs) if pairs is not None else gold.GOLD_PAIRS
    return {view: _separability_for(view, chosen, vectors_by_text) for view in ("action", "object")}


@dataclass(frozen=True, slots=True)
class Ceiling:
    """The best any method on these features could do, measured not assumed.

    Separability above answers whether a *single threshold on raw similarity*
    works. It cannot say whether the information is absent from the features or
    merely not exposed by distance. A leave-one-out 1-nearest-neighbour on the
    gold labels does: it is the most generous thing the representation can do,
    because it is allowed to see the answer.

    If the supervised ceiling is also low, the labels or the representation are
    at fault and no encoder will fix it. If the ceiling is high while the
    threshold score is low, the information is there and a *learned* method -
    which is what the action/object projection would be - could get at it.
    """

    view: str
    supervised_balanced_accuracy: float
    labelled_pairs: int

    def as_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "supervised_balanced_accuracy": self.supervised_balanced_accuracy,
            "labelled_pairs": self.labelled_pairs,
        }


def supervised_ceiling(
    vectors_by_text: dict[str, np.ndarray],
    pairs: Sequence[GoldPair],
    view: str,
) -> Ceiling:
    """Leave-one-out 1-NN balanced accuracy on the gold labels.

    Each pair contributes its two strings as two items carrying the pair's label.
    Predicting an item means the most similar *other* item should carry the same
    label. This is not a method anyone would ship - it needs the labels - but it
    is the honest ceiling for what these vectors encode.
    """
    attribute = f"same_{view}"
    items: list[np.ndarray] = []
    labels: list[int] = []
    for pair in pairs:
        verdict = getattr(pair, attribute)
        if verdict is None:
            continue
        # Two strings per pair and they share one label, so the label is appended
        # twice. Appending it once made `labels` half the length of `items` and
        # the nearest-neighbour lookup indexed out of bounds.
        items.append(vectors_by_text[pair.a])
        items.append(vectors_by_text[pair.b])
        labels.extend([1 if verdict else 0, 1 if verdict else 0])
    if not items:
        raise ValueError(f"view {view!r} has no decided pairs")
    if len(items) != len(labels):
        raise ValueError(f"{len(items)} items but {len(labels)} labels")

    matrix = np.stack(items)
    truth = np.array(labels)
    similarity = matrix @ matrix.T
    # Own row is the only one to exclude; everything else is fair game.
    np.fill_diagonal(similarity, -np.inf)

    positives = truth == 1
    negatives = ~positives
    correct = np.zeros(len(truth), dtype=bool)
    for index in range(len(truth)):
        correct[index] = truth[int(np.argmax(similarity[index]))] == truth[index]

    true_positive = float(correct[positives].sum())
    true_negative = float(correct[negatives].sum())
    balanced = 0.5 * (
        (true_positive / int(positives.sum()) if positives.any() else 0.0)
        + (true_negative / int(negatives.sum()) if negatives.any() else 0.0)
    )
    return Ceiling(
        view=view,
        supervised_balanced_accuracy=float(balanced),
        labelled_pairs=len(truth),
    )


def ceilings(
    vectors_by_text: dict[str, np.ndarray],
    pairs: Sequence[GoldPair] | None = None,
) -> dict[str, Ceiling]:
    from experiments.clustering.evaluation import gold

    chosen = tuple(pairs) if pairs is not None else gold.GOLD_PAIRS
    return {
        view: supervised_ceiling(vectors_by_text, chosen, view) for view in ("action", "object")
    }


def matrix_for(vectors_by_text: dict[str, np.ndarray], texts: Sequence[str]) -> np.ndarray:
    """Stack vectors in a caller-chosen order, for reuse by other steps."""
    return np.stack([vectors_by_text[text] for text in texts])


def all_vectors(vectors: np.ndarray, texts: Sequence[str]) -> dict[str, np.ndarray]:
    return dict(zip(texts, vectors, strict=True))


def vocabulary() -> tuple[str, ...]:
    return vocab()
