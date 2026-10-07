"""A learned per-view projection: turn the encoder's space into a clustering space."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from experiments.clustering.evaluation.gold import GoldPair

DEFAULT_SHRINKAGE = 0.9

K_CHOICES = (1, 2, 4, 8, 16)


@dataclass(frozen=True, slots=True)
class Projection:
    """A fitted linear map from encoder space to clustering space."""

    view: str
    encoder: str
    centre: np.ndarray
    basis: np.ndarray
    eigenvalues: tuple[float, ...]
    shrinkage: float

    @property
    def k(self) -> int:
        return int(self.basis.shape[0])

    def project(self, vectors_by_text: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Apply the projection."""
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
    """Dev pair differences, split into the two classes, keyed by the pair's label."""
    attribute = f"same_{view}"
    same: list[np.ndarray] = []
    different: list[np.ndarray] = []
    for pair in pairs:
        verdict = getattr(pair, attribute)
        if verdict is None:
            continue
        delta = vectors[pair.a] - vectors[pair.b]
        if not np.any(delta):
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
    """Symmetric inverse square root via eigendecomposition."""
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
    """Top-`k` whitened directions, and their eigenvalues."""
    dim = within.shape[0]
    regularised = within + shrinkage * np.trace(within) / dim * np.eye(dim)
    whitener = _inv_sqrt(regularised)
    total: np.ndarray = whitener @ (within + between) @ whitener
    symmetric: np.ndarray = 0.5 * (total + total.T)
    values, vectors = np.linalg.eigh(symmetric)
    order = np.argsort(values)[::-1][:k]
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
    """Pick the projection width on dev pairs, and return the whole sweep."""
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
