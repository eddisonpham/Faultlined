"""Can the embeddings separate the two classes at all?"""

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
    metric: str
    positive_min: float
    positive_mean: float
    negative_max: float
    negative_mean: float
    best_threshold: float
    best_accuracy: float
    false_merges: int
    false_splits: int
    clean_threshold: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "metric": self.metric,
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


METRICS = ("cosine", "euclidean")


def _similarity(vectors: dict[str, np.ndarray], a: str, b: str, metric: str) -> float:
    """Signed closeness: larger means closer, whichever metric was asked for."""
    if metric == "cosine":
        return float(vectors[a] @ vectors[b])
    left = vectors[a]
    right = vectors[b]
    delta: np.ndarray = left - right
    distance: float = float(np.sqrt(delta @ delta))
    return -distance


def _separability_for(
    view: str,
    pairs: Sequence[GoldPair],
    vectors: dict[str, np.ndarray],
    metric: str = "cosine",
) -> ViewSeparability:
    attribute = f"same_{view}"
    positives = [
        _similarity(vectors, p.a, p.b, metric) for p in pairs if getattr(p, attribute) is True
    ]
    negatives = [
        _similarity(vectors, p.a, p.b, metric) for p in pairs if getattr(p, attribute) is False
    ]
    if not positives or not negatives:
        raise ValueError(f"view {view!r} has no decided pairs of both classes")

    pos = np.array(sorted(positives))
    neg = np.array(sorted(negatives))

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

    clean = float(pos.min()) if float(neg.max()) < float(pos.min()) else None

    return ViewSeparability(
        view=view,
        positives=len(pos),
        metric=metric,
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
    metric: str = "cosine",
) -> dict[str, ViewSeparability]:
    """Measure per-view separability."""
    from experiments.clustering.evaluation import gold

    chosen = tuple(pairs) if pairs is not None else gold.GOLD_PAIRS
    return {
        view: _separability_for(view, chosen, vectors_by_text, metric)
        for view in ("action", "object")
    }


@dataclass(frozen=True, slots=True)
class Probe:
    """How far a *learned* method could get on these vectors, measured not assumed."""

    view: str
    dev_cv_balanced_accuracy: float
    dev_balanced_accuracy: float
    heldout_balanced_accuracy: float
    alpha: float
    dev_pairs: int
    heldout_pairs: int

    @property
    def headroom(self) -> float:
        """Held-out balanced accuracy above the 0.5 a coin flip would score."""
        return self.heldout_balanced_accuracy - 0.5

    @property
    def generalisation_gap(self) -> float:
        return self.dev_cv_balanced_accuracy - self.heldout_balanced_accuracy

    def as_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "dev_cv_balanced_accuracy": self.dev_cv_balanced_accuracy,
            "dev_balanced_accuracy": self.dev_balanced_accuracy,
            "heldout_balanced_accuracy": self.heldout_balanced_accuracy,
            "headroom": self.headroom,
            "generalisation_gap": self.generalisation_gap,
            "alpha": self.alpha,
            "dev_pairs": self.dev_pairs,
            "heldout_pairs": self.heldout_pairs,
        }


_ALPHAS = (0.01, 0.1, 1.0, 10.0, 100.0)


def _features(
    vectors: dict[str, np.ndarray], pairs: Sequence[GoldPair], view: str
) -> tuple[np.ndarray, np.ndarray]:
    """Difference vectors plus their length, with +-1 targets, globally scaled."""
    attribute = f"same_{view}"
    rows: list[np.ndarray] = []
    lengths: list[float] = []
    targets: list[float] = []
    for pair in pairs:
        verdict = getattr(pair, attribute)
        if verdict is None:
            continue
        difference = vectors[pair.a] - vectors[pair.b]
        norm = float(np.linalg.norm(difference))
        if norm == 0.0:
            continue
        rows.append(difference)
        lengths.append(norm)
        targets.append(1.0 if verdict else -1.0)
    if not rows:
        raise ValueError(f"view {view!r} has no decided pairs")

    stacked = np.stack(rows)
    scale = float(np.sqrt((stacked**2).sum(axis=1).mean()))
    if scale > 0.0:
        stacked = stacked / scale
    length_column = np.asarray(lengths, dtype=float).reshape(-1, 1)
    length_scale = float(np.sqrt((length_column**2).mean()))
    if length_scale > 0.0:
        length_column = length_column / length_scale
    features = np.hstack([stacked, length_column, np.ones((len(stacked), 1))])
    return features, np.array(targets)


def _balanced_accuracy(scores: np.ndarray, targets: np.ndarray) -> float:
    """Balanced accuracy at sign zero."""
    positives = targets > 0
    negatives = ~positives
    if not positives.any() or not negatives.any():
        raise ValueError("probe scoring needs both classes")
    return 0.5 * (float((scores[positives] > 0).mean()) + float((scores[negatives] <= 0).mean()))


def _loo_scores(kernel: np.ndarray, targets: np.ndarray, alpha: float) -> np.ndarray:
    """Leave-one-out decision residuals for ridge, in closed form."""
    regularised = kernel + alpha * np.eye(len(kernel))
    inverse: np.ndarray = np.linalg.solve(regularised, np.eye(len(kernel)))
    fitted: np.ndarray = kernel @ (inverse @ targets)
    one_minus_s: np.ndarray = alpha * np.diag(inverse)
    residual: np.ndarray = (targets - fitted) / one_minus_s
    return residual


def _group_cv_score(
    kernel: np.ndarray, targets: np.ndarray, groups: np.ndarray, alpha: float
) -> float:
    """Leave-one-*component*-out balanced accuracy."""
    scores = np.empty(len(targets), dtype=float)
    for group in np.unique(groups):
        held = groups == group
        train = ~held
        if not train.any() or not held.any():  # pragma: no cover - defensive
            continue
        sub = kernel[np.ix_(train, train)] + alpha * np.eye(int(train.sum()))
        dual: np.ndarray = np.linalg.solve(sub, targets[train])
        scores[held] = kernel[np.ix_(held, train)] @ dual
    return _balanced_accuracy(scores, targets)


def probe(
    vectors_by_text: dict[str, np.ndarray],
    dev: Sequence[GoldPair],
    heldout: Sequence[GoldPair],
    view: str,
    dev_groups: Sequence[int] = (),
) -> Probe:
    """Fit a linear probe on dev pairs, score it on held-out pairs."""
    dev_x, dev_y = _features(vectors_by_text, dev, view)
    heldout_x, heldout_y = _features(vectors_by_text, heldout, view)

    gram = dev_x @ dev_x.T
    eye = np.eye(len(dev_x))
    if len(dev_groups) == len(dev_x):
        fold_units: np.ndarray = np.asarray(dev_groups)
    else:
        fold_units = np.arange(len(dev_x))
    best: tuple[float, float] | None = None
    for alpha in _ALPHAS:
        cv_score = _group_cv_score(gram, dev_y, fold_units, alpha)
        if best is None or cv_score > best[0]:
            best = (cv_score, alpha)
    if best is None:  # pragma: no cover - _ALPHAS is a non-empty constant
        raise ValueError(f"view {view!r} fitted no probe")

    cv_score, alpha = best
    weights = dev_x.T @ np.linalg.solve(gram + alpha * eye, dev_y)
    dev_score = _balanced_accuracy(dev_x @ weights, dev_y)
    heldout_score = _balanced_accuracy(heldout_x @ weights, heldout_y)
    return Probe(
        view=view,
        dev_cv_balanced_accuracy=float(cv_score),
        dev_balanced_accuracy=float(dev_score),
        heldout_balanced_accuracy=float(heldout_score),
        alpha=float(alpha),
        dev_pairs=len(dev_x),
        heldout_pairs=len(heldout_x),
    )


def probes(
    vectors_by_text: dict[str, np.ndarray],
    dev: Sequence[GoldPair],
    heldout: Sequence[GoldPair],
    dev_groups: Sequence[int] = (),
) -> dict[str, Probe]:
    return {
        view: probe(vectors_by_text, dev, heldout, view, dev_groups)
        for view in ("action", "object")
    }


def matrix_for(vectors_by_text: dict[str, np.ndarray], texts: Sequence[str]) -> np.ndarray:
    """Stack vectors in a caller-chosen order, for reuse by other steps."""
    return np.stack([vectors_by_text[text] for text in texts])


def all_vectors(vectors: np.ndarray, texts: Sequence[str]) -> dict[str, np.ndarray]:
    return dict(zip(texts, vectors, strict=True))


def vocabulary() -> tuple[str, ...]:
    return vocab()
