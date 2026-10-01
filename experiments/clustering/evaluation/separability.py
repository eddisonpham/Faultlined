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

It also reports a linear **probe** fitted on dev pairs and scored on held-out
pairs. That second number is what distinguishes "the information is not in these
vectors" from "a single threshold cannot reach it", and it is the budget any
learned action/object projection has to work within.

Both measurements are reported per view, because that is the whole reason for the
two-view design: the action view may be separable while the object view is not,
or the reverse, and that asymmetry is the finding that decides whether clustering
on embeddings is viable at all.
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
    #: Which distance the similarities below are measured by.
    metric: str
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


#: How a pair's closeness is measured. `cosine` is the right default for raw
#: embeddings, which are unit vectors where cosine *is* a distance. `euclidean`
#: exists because a learned projection deliberately changes the metric's shape -
#: it whitens, equalising variance along every retained axis - and cosine of the
#: result is not the distance that projection optimises. In one dimension cosine is
#: degenerate outright, so a 1-axis projection cannot be scored with it at all.
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
    """Measure per-view separability. Uses the full gold set, both splits.

    This is a premise check rather than a scored result: it reports where the two
    classes sit, so it is informative on every pair and does not need a split.
    Deciding a threshold *from* it is a different act, and that is what the dev
    split is for.
    """
    from experiments.clustering.evaluation import gold

    chosen = tuple(pairs) if pairs is not None else gold.GOLD_PAIRS
    return {
        view: _separability_for(view, chosen, vectors_by_text, metric)
        for view in ("action", "object")
    }


@dataclass(frozen=True, slots=True)
class Probe:
    """How far a *learned* method could get on these vectors, measured not assumed.

    Separability above answers whether a **single threshold on raw similarity**
    works. It cannot say whether the information is absent from the features or
    merely not exposed by distance. This does: it fits a linear probe on pair
    differences and scores it on pairs the fit never saw.

    Read it as the budget for a learned projection. If the held-out probe is near
    chance, no projection over these vectors can work and the encoder is the
    wrong place to spend effort. If it is high while the threshold score is low,
    the information is there and a projection is worth building.
    """

    view: str
    #: Leave-one-component-out balanced accuracy inside dev. This is what chose
    #: `alpha`, and it is the honest estimate of what the probe does on unseen
    #: material - the training score next to it only shows the size of the gap.
    dev_cv_balanced_accuracy: float
    #: Balanced accuracy on the pairs the probe was fitted on. Shown to expose
    #: overfitting, never quoted as a result.
    dev_balanced_accuracy: float
    #: Balanced accuracy on the held-out pairs. This is the number to quote; the
    #: dev number only shows how much of it is overfitting.
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


#: Ridge strengths tried on dev. Small values overfit the pair set, large ones
#: collapse to the mean; both ends are reported through the dev/held-out gap.
_ALPHAS = (0.01, 0.1, 1.0, 10.0, 100.0)


def _features(
    vectors: dict[str, np.ndarray], pairs: Sequence[GoldPair], view: str
) -> tuple[np.ndarray, np.ndarray]:
    """Difference vectors plus their length, with +-1 targets, globally scaled.

    The difference of the two vectors is the natural feature for "are these the
    same": it is antisymmetric, so swapping the pair flips the sign rather than
    silently changing the answer.

    Two things about the magnitude, both learned the hard way:

    Normalising each row away throws out the primary signal. For unit-norm
    embeddings the length of the difference is a monotone function of cosine
    distance - the same evidence - so scaling it out leaves only the direction,
    which says less.

    Passing the difference alone is still not enough, because magnitude is not a
    linear function of the difference. A linear probe needs one fixed direction to
    score highly in; if two near-identical strings sit close together but along
    some arbitrary axis, no such direction exists and the probe scores chance on a
    distinction a cosine threshold would have caught. So the length is appended as
    its own column. That guarantees the probe is at least as informative as the
    threshold it is meant to improve on - a probe that loses to a threshold would
    be an artefact of the features, not a fact about the encoder.

    A constant column closes the last gap. The decision rule is "score above
    zero", so without an intercept the boundary is pinned to the origin and no
    weight can separate *small* values from *large* ones - a coefficient on the
    length column alone scores both classes the same sign and lands at exactly
    0.500. With an intercept the boundary can sit between a paraphrase's near-zero
    difference and a different task's large one.

    A single global scale is applied to the non-constant columns, so the ridge
    penalty is not chasing the units of whatever encoder produced the vectors.
    """
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
            # Identical vectors carry no evidence either way, and a zero row
            # would contribute a divide-by-zero later.
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
    """Balanced accuracy at sign zero.

    The features carry a constant column, so zero is a real fitted intercept
    rather than a forced boundary through the origin, and the model predicts
    "same" exactly when it scores above zero. There is no threshold to tune here,
    which matters: tuning one on the held-out pairs would quietly spend the
    held-out set.
    """
    positives = targets > 0
    negatives = ~positives
    if not positives.any() or not negatives.any():
        raise ValueError("probe scoring needs both classes")
    return 0.5 * (float((scores[positives] > 0).mean()) + float((scores[negatives] <= 0).mean()))


def _loo_scores(kernel: np.ndarray, targets: np.ndarray, alpha: float) -> np.ndarray:
    """Leave-one-out decision residuals for ridge, in closed form.

    Refitting once per dev pair is affordable but pointless: for a kernel ridge
    fit the hat matrix gives every leave-one-out residual directly as
    ``(y_i - f_i) / (1 - h_ii)``. One solve, all the folds.

    Validated against explicit per-fold refitting in
    `tests/unit/test_clustering_methods.py`; taking ``1 - h_ii`` from the solved
    dual weights instead of the inverse diagonal scores every model at exactly
    0.500.
    """
    regularised = kernel + alpha * np.eye(len(kernel))
    inverse: np.ndarray = np.linalg.solve(regularised, np.eye(len(kernel)))
    fitted: np.ndarray = kernel @ (inverse @ targets)
    # The smoother is S = K(K+aI)^-1 = I - a(K+aI)^-1, so 1 - S_ii is the scaled
    # diagonal of the inverse.
    one_minus_s: np.ndarray = alpha * np.diag(inverse)
    residual: np.ndarray = (targets - fitted) / one_minus_s
    return residual


def _group_cv_score(
    kernel: np.ndarray, targets: np.ndarray, groups: np.ndarray, alpha: float
) -> float:
    """Leave-one-*component*-out balanced accuracy.

    Plain leave-one-pair-out scores 1.000 on the constructed dev set, which is not
    a good result but a contaminated one: 195 dev pairs sit in roughly fifty
    leakage components, so holding out one pair leaves its paraphrases in training
    and the model simply recognises the task. The component is the unit of
    independence this whole harness already uses for the split, so it is the unit
    the fold uses too.
    """
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
    """Fit a linear probe on dev pairs, score it on held-out pairs.

    A closed-form ridge regression rather than a logistic solver: the answer is a
    linear separability question, the fit is exact and deterministic, and it adds
    no dependency to an experiment package that is otherwise numpy alone.

    The label belongs to the **pair**, which is why this is not a per-string
    nearest-neighbour score. A string appears in many pairs carrying opposite
    labels - "pick up the red cube" is the same action as "grab the red cube" and
    a different action from "push the red cube" - so a per-string classifier is
    being asked a question with no consistent answer and scores at chance. That
    was the first implementation's bug, and it reported 0.478 on the action view
    where the real answer is well above chance.
    """
    dev_x, dev_y = _features(vectors_by_text, dev, view)
    heldout_x, heldout_y = _features(vectors_by_text, heldout, view)

    gram = dev_x @ dev_x.T
    eye = np.eye(len(dev_x))
    if len(dev_groups) == len(dev_x):
        fold_units: np.ndarray = np.asarray(dev_groups)
    else:
        # No group information supplied: fall back to per-pair folds rather than
        # inventing an independence the caller did not provide.
        fold_units = np.arange(len(dev_x))
    # (CV score, alpha). alpha is chosen by cross-validation *inside dev*, so
    # neither the dev pairs nor the held-out pairs influence it; held-out is read
    # once, at the end, for the alpha already fixed.
    best: tuple[float, float] | None = None
    for alpha in _ALPHAS:
        cv_score = _group_cv_score(gram, dev_y, fold_units, alpha)
        if best is None or cv_score > best[0]:
            best = (cv_score, alpha)
    if best is None:  # pragma: no cover - _ALPHAS is a non-empty constant
        raise ValueError(f"view {view!r} fitted no probe")

    cv_score, alpha = best
    # Solved in sample space, which is the cheap form: the Gram matrix is
    # n-by-n rather than 256-by-256. That yields dual weights over the dev
    # pairs, so they are mapped back to feature space before scoring.
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
