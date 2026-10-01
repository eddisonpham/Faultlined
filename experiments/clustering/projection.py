"""A learned per-view projection: turn the encoder's space into a clustering space.

EXP-2.5-02 found the two views need opposite treatment. The object view is already
separable by a threshold on MiniLM (0.956, ten false merges) and a projection has
nothing to add. The action view is not: the best threshold manages 0.679 while a
linear probe on the same vectors reaches 0.736, and the constructed set says the
information is there. So the action view needs a learned metric, and this is it.

**What is learned.** Not a new embedding model - a linear map, fitted on labelled
pairs, that reshapes the existing space so that a distance threshold *can* express
what the probe already finds. Concretely it whitens away the directions along which
same-task strings vary (re-wording, colour adjectives, a trailing preposition) and
keeps the directions along which different tasks separate. That is Fisher
discriminant analysis applied to pair differences, and it is the smallest thing
that turns a probe into something a centroid rule can consume.

**Why differences and not labels.** There are no per-string labels, and inventing
them is the trap the old ceiling diagnostic fell into: "pick up the red cube" is the
same action as "grab the red cube" and a different action from "push the red cube",
so any per-string label is a fiction. Pair differences dodge this entirely, because
the label "same action" is a statement about a pair and nothing else.

**Fitted on dev only.** Every quantity here - the within-class scatter, the
discriminative directions, the choice of `k` - is estimated from dev pairs alone.
Held-out pairs are scored once, at the end, with everything already frozen. A
projection that had seen its own test set would produce exactly the kind of
confident, meaningless number this whole stage has been guarding against.

**The first direction is LDA; the rest are not.** With two classes the between-class
scatter is rank one, so a single whitened direction *is* the Fisher axis. Additional
directions are the largest remaining within-class variance, which is noise by
construction. They are still selectable because whether the extra axes help or hurt
is an empirical question, and `choose_k` answers it on dev rather than by argument.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from experiments.clustering.evaluation.gold import GoldPair

#: How much identity to mix into the within-class scatter.
#:
#: The estimate comes from a few hundred dev pairs in up to 384 dimensions, so it is
#: rank-deficient by construction and its inverse is meaningless without this. The
#: value is the most favourable one measured, not a tuned default: at 0.1 held-out
#: separability collapses (action 0.724 -> 0.598), and raising it towards 1 recovers
#: the unprojected baseline without ever beating it. See EXP-2.5-03.
DEFAULT_SHRINKAGE = 0.9

#: Candidate projection widths. The first axis is the Fisher direction; anything
#: past it is added variance, so the useful answer is likely to be small and the
#: sweep exists to establish that rather than assume it.
K_CHOICES = (1, 2, 4, 8, 16)


@dataclass(frozen=True, slots=True)
class Projection:
    """A fitted linear map from encoder space to clustering space."""

    view: str
    encoder: str
    #: Mean of the dev pair differences; differences are centred on it before
    #: projecting, so the discriminative direction is not confused with the offset.
    centre: np.ndarray
    #: Rows are the retained directions, orthonormal in the whitened metric.
    basis: np.ndarray
    #: Whitened eigenvalues of each retained direction, largest first. Reported so
    #: a reader can see whether the later directions carry signal at all.
    eigenvalues: tuple[float, ...]
    shrinkage: float

    @property
    def k(self) -> int:
        return int(self.basis.shape[0])

    def project(self, vectors_by_text: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Apply the projection. Unseen strings are fine; they were never needed."""
        return {
            text: ((vector - self.centre) @ self.basis.T).astype(np.float64)
            for text, vector in vectors_by_text.items()
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "view": self.view,
            "encoder": self.encoder,
            "k": self.k,
            "shrinkage": self.shrinkage,
            "eigenvalues": [round(value, 6) for value in self.eigenvalues],
        }


def _differences(
    vectors: dict[str, np.ndarray], pairs: Sequence[GoldPair], view: str
) -> tuple[np.ndarray, np.ndarray]:
    """Dev pair differences, split into the two classes, keyed by the pair's label.

    Returns `(same, different)` rather than one matrix with labels, because the
    Fisher construction needs the two populations separately and the caller should
    not have to re-split them.
    """
    attribute = f"same_{view}"
    same: list[np.ndarray] = []
    different: list[np.ndarray] = []
    for pair in pairs:
        verdict = getattr(pair, attribute)
        if verdict is None:
            continue
        delta = vectors[pair.a] - vectors[pair.b]
        if not np.any(delta):
            # Identical vectors: no direction to learn from.
            continue
        (same if verdict else different).append(delta)
    if not same or not different:
        raise ValueError(f"fitting a {view!r} projection needs both classes of decided dev pairs")
    return np.stack(same), np.stack(different)


def _covariance(rows: np.ndarray, mean: np.ndarray) -> np.ndarray:
    if len(rows) < 2:
        return np.zeros((rows.shape[1], rows.shape[1]), dtype=np.float64)
    centred = rows - mean
    gram: np.ndarray = centred.T @ centred
    result: np.ndarray = gram / len(rows)
    return result


def _inv_sqrt(matrix: np.ndarray) -> np.ndarray:
    """Symmetric inverse square root via eigendecomposition.

    A Cholesky factorisation would work for the solve but not for the symmetric
    whitening this needs, and `eigh` is deterministic given the same matrix, which
    keeps the whole experiment reproducible.
    """
    eigenvalues: np.ndarray
    eigenvectors: np.ndarray
    eigenvalues, eigenvectors = np.linalg.eigh(matrix)
    floor = max(float(eigenvalues.max()), 1e-12) * 1e-10
    scaled: np.ndarray = eigenvectors / np.sqrt(np.maximum(eigenvalues, floor))
    root: np.ndarray = scaled @ eigenvectors.T
    return root


def _directions(
    within: np.ndarray,
    between: np.ndarray,
    k: int,
    shrinkage: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Top-`k` whitened directions, and their eigenvalues.

    Whitening by the within-class scatter is what removes the nuisance axes; the
    eigenproblem is then run on `within + between`, whose leading eigenvector is the
    Fisher axis and whose trailing ones are the largest remaining nuisance. Taking
    the top `k` of that spectrum is therefore "the discriminative axis plus the
    `k-1` strongest others", and `k=1` is exactly LDA.
    """
    dim = within.shape[0]
    regularised = within + shrinkage * np.trace(within) / dim * np.eye(dim)
    whitener = _inv_sqrt(regularised)
    total: np.ndarray = whitener @ (within + between) @ whitener
    # Symmetrise: the product is symmetric in exact arithmetic and not otherwise,
    # and `eigh` reads only one triangle, so an asymmetry here is silently ignored.
    symmetric: np.ndarray = 0.5 * (total + total.T)
    values, vectors = np.linalg.eigh(symmetric)
    order = np.argsort(values)[::-1][:k]
    # `Projection.basis` is documented as one direction per row, so the `k` by `dim`
    # result is transposed here rather than at every use site.
    basis: np.ndarray = (whitener @ vectors[:, order]).T
    return basis, values[order]


def fit(
    vectors: dict[str, np.ndarray],
    dev: Sequence[GoldPair],
    view: str,
    encoder: str,
    k: int,
    shrinkage: float = DEFAULT_SHRINKAGE,
) -> Projection:
    """Fit a projection on dev pairs only."""
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    same, different = _differences(vectors, dev, view)
    mean_same = same.mean(axis=0)
    mean_different = different.mean(axis=0)

    within = 0.5 * (_covariance(same, mean_same) + _covariance(different, mean_different))
    # The discriminative direction is the gap between the two populations, centred
    # so that projecting the *strings* is the same operation as projecting their
    # differences.
    gap = mean_same - mean_different
    between = np.outer(gap, gap)

    basis, eigenvalues = _directions(within, between, k, shrinkage)
    return Projection(
        view=view,
        encoder=encoder,
        centre=(mean_same + mean_different) / 2.0,
        basis=basis,
        eigenvalues=tuple(float(value) for value in eigenvalues),
        shrinkage=shrinkage,
    )


def fit_all(
    vectors: dict[str, np.ndarray],
    dev: Sequence[GoldPair],
    encoder: str,
    k: int,
    shrinkage: float = DEFAULT_SHRINKAGE,
) -> dict[str, Projection]:
    return {view: fit(vectors, dev, view, encoder, k, shrinkage) for view in ("action", "object")}


@dataclass(frozen=True, slots=True)
class Choice:
    """One candidate width and what it scored on dev."""

    k: int
    dev_balanced_accuracy: float
    dev_false_merges: int
    dev_false_splits: int
    eigenvalues: tuple[float, ...]

    def as_dict(self) -> dict[str, object]:
        # `slots=True` dataclasses have no `__dict__`, so the sweep is serialised
        # through an explicit mapping.
        return {
            "k": self.k,
            "dev_balanced_accuracy": self.dev_balanced_accuracy,
            "dev_false_merges": self.dev_false_merges,
            "dev_false_splits": self.dev_false_splits,
            "eigenvalues": [round(value, 6) for value in self.eigenvalues],
        }


def choose_k(
    vectors: dict[str, np.ndarray],
    dev: Sequence[GoldPair],
    view: str,
    encoder: str,
    shrinkage: float = DEFAULT_SHRINKAGE,
    candidates: Sequence[int] = K_CHOICES,
    metric: str = "euclidean",
) -> tuple[Projection, tuple[Choice, ...]]:
    """Pick the projection width on dev pairs, and return the whole sweep.

    The score is **threshold** balanced accuracy on dev, not the probe. The probe
    is what motivated the projection; optimising it here would be optimising the
    thing the projection is a substitute for. The goal is that after projecting, a
    centroid rule and a radius can do the work, so a radius is what gets scored.

    The metric defaults to Euclidean because that is the distance the projection
    optimises. Whitening rescales every retained axis to unit variance, which
    flattens the difference between "close because same task" and "close because
    near"; cosine of the projected vectors is then not the quantity that was
    arranged, and in one axis cosine is degenerate outright. Scoring the whitened
    space with cosine measured 0.509 on the action view - worse than the 0.724 it
    started from - which says more about the mismatch than about the projection.

    Ties break towards the narrower projection. Every axis past the first is
    variance the labels never asked for, so the smaller model is the one to keep
    when the evidence does not separate them.
    """
    from experiments.clustering.evaluation import separability

    rows: list[Choice] = []
    fitted: dict[int, Projection] = {}
    for k in candidates:
        candidate = fit(vectors, dev, view, encoder, k, shrinkage)
        projected = candidate.project(vectors)
        report = separability.separability(projected, dev, metric=metric)[view]
        fitted[k] = candidate
        rows.append(
            Choice(
                k=k,
                dev_balanced_accuracy=report.best_accuracy,
                dev_false_merges=report.false_merges,
                dev_false_splits=report.false_splits,
                eigenvalues=candidate.eigenvalues,
            )
        )

    best = max(rows, key=lambda row: (row.dev_balanced_accuracy, -row.k))
    return fitted[best.k], tuple(rows)
