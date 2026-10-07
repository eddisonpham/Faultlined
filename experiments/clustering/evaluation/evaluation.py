"""Turning a clustering run into scores the reports can compare."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from experiments.clustering.evaluation import metrics
from experiments.clustering.evaluation.gold import GoldPair


def label_map(labels: Sequence[int], texts: Sequence[str]) -> dict[str, int]:
    """Pair a positional label vector with the texts it describes."""
    if len(labels) != len(texts):
        raise ValueError(f"{len(labels)} labels for {len(texts)} texts")
    return dict(zip(texts, labels, strict=True))


def pair_scorer(
    subset: Sequence[GoldPair],
    view: str,
) -> Callable[[Sequence[int], Sequence[str], str], metrics.PairScores]:
    """Build a scorer for one split and one view."""
    scored = tuple(subset)

    def score(labels: Sequence[int], texts: Sequence[str], _context: str) -> metrics.PairScores:
        return metrics.pair_scores(label_map(labels, texts), scored, view)

    return score


def score_labels(
    labels: dict[str, int],
    pairs: Sequence[GoldPair],
    view: str,
) -> metrics.PairScores:
    """Score an already-materialised label mapping against the gold pairs."""
    return metrics.pair_scores(labels, pairs, view)
