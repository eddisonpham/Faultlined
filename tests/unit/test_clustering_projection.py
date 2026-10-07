"""Tests for the learned projection and for the distance metrics it is scored with."""

from __future__ import annotations

import numpy as np
import pytest
from experiments.clustering import projection
from experiments.clustering.evaluation import gold, separability


def _symmetric(matrix: np.ndarray) -> bool:
    return bool(np.allclose(matrix, matrix.T, atol=1e-8))


def _unit(value: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(value))
    return value / norm if norm else value


def test_the_inverse_square_root_is_symmetric_and_inverts() -> None:
    rng = np.random.default_rng(2)
    matrix = rng.normal(size=(6, 6))
    matrix = matrix @ matrix.T + 0.5 * np.eye(6)

    root = projection._inv_sqrt(matrix)

    assert _symmetric(root)
    product: np.ndarray = root @ matrix @ root
    assert np.allclose(product, np.eye(6), atol=1e-8)


def test_the_inverse_square_root_survives_a_singular_matrix() -> None:
    """The dev covariance is rank-deficient by construction, so this is the normal case."""
    rng = np.random.default_rng(6)
    basis = rng.normal(size=(6, 2))
    singular = basis @ basis.T

    root = projection._inv_sqrt(singular)

    assert np.isfinite(root).all()
    assert root.shape == (6, 6)


def test_one_axis_is_the_fisher_direction() -> None:
    """`k=1` should recover the textbook Fisher direction."""
    rng = np.random.default_rng(8)
    dim = 5
    same = rng.normal(size=(40, dim)) + np.array([0.0, 3.0, 0.0, 0.0, 0.0])
    different = rng.normal(size=(40, dim)) - np.array([0.0, 3.0, 0.0, 0.0, 0.0])
    within = 0.5 * (
        projection._covariance(same, same.mean(axis=0))
        + projection._covariance(different, different.mean(axis=0))
    )
    gap = same.mean(axis=0) - different.mean(axis=0)

    basis, eigenvalues = projection._directions(within, np.outer(gap, gap), 1, 0.0)

    assert basis.shape == (1, dim)
    lda_axis: np.ndarray = np.linalg.solve(within, gap)
    direction = basis[0] / np.linalg.norm(basis[0])
    alignment = abs(float(direction @ lda_axis)) / float(np.linalg.norm(lda_axis))
    assert alignment == pytest.approx(1.0, abs=1e-6)
    assert eigenvalues[0] > 0


def test_more_axes_means_more_dimensions_and_never_fewer() -> None:
    rng = np.random.default_rng(9)
    vectors = {f"t{i}": _unit(rng.normal(size=8)) for i in range(12)}
    pairs = tuple(gold.GoldPair(f"t{i}", f"t{i + 1}", i % 2 == 0, True) for i in range(0, 10, 2))
    vectors.update({f"u{i}": _unit(rng.normal(size=8)) for i in range(12)})
    both = pairs + tuple(gold.GoldPair(f"t{i}", f"u{i}", False, True) for i in range(0, 10, 2))

    narrow = projection.fit(vectors, both, "action", "test", 1)
    wide = projection.fit(vectors, both, "action", "test", 4)

    assert narrow.k == 1
    assert wide.k == 4
    assert set(narrow.project(vectors)) == set(vectors)
    assert all(vector.shape == (4,) for vector in wide.project(vectors).values())


def test_projection_is_deterministic() -> None:
    rng = np.random.default_rng(10)
    vectors = {f"t{i}": _unit(rng.normal(size=8)) for i in range(20)}
    vectors.update({f"u{i}": _unit(rng.normal(size=8)) for i in range(20)})
    pairs = tuple(gold.GoldPair(f"t{i}", f"t{i + 2}", True, True) for i in range(0, 18, 4)) + tuple(
        gold.GoldPair(f"t{i}", f"u{i}", False, False) for i in range(0, 18, 4)
    )

    first = projection.fit(vectors, pairs, "action", "test", 2)
    second = projection.fit(vectors, pairs, "action", "test", 2)

    assert np.allclose(first.basis, second.basis)
    assert first.eigenvalues == second.eigenvalues


def test_fitting_needs_both_classes() -> None:
    vectors = {"a": np.ones(4), "b": np.zeros(4), "c": np.ones(4) * 2}
    pairs = (gold.GoldPair("a", "b", True, True),)
    with pytest.raises(ValueError, match="both classes"):
        projection.fit(vectors, pairs, "action", "test", 1)


def test_k_must_be_positive() -> None:
    vectors = {"a": np.ones(4), "b": np.zeros(4)}
    pairs = (gold.GoldPair("a", "b", True, True), gold.GoldPair("b", "a", False, False))
    with pytest.raises(ValueError, match="at least 1"):
        projection.fit(vectors, pairs, "action", "test", 0)


def test_both_metrics_rank_unit_vectors_identically() -> None:
    """On unit vectors cosine and Euclidean are the same ordering."""
    rng = np.random.default_rng(12)
    vectors = {f"s{i}": _unit(rng.normal(size=16)) for i in range(40)}
    pairs = tuple(gold.GoldPair(f"s{i}", f"s{i + 1}", True, True) for i in range(0, 20, 2)) + tuple(
        gold.GoldPair(f"s{i}", f"s{i + 20}", False, False) for i in range(0, 20, 2)
    )

    by_cosine = separability.separability(vectors, pairs, metric="cosine")["action"]
    by_euclidean = separability.separability(vectors, pairs, metric="euclidean")["action"]

    assert by_cosine.best_accuracy == pytest.approx(by_euclidean.best_accuracy, abs=1e-9)
    assert by_cosine.false_merges == by_euclidean.false_merges
    assert by_cosine.false_splits == by_euclidean.false_splits


def test_cosine_is_degenerate_in_one_dimension() -> None:
    """Why the projected space cannot be scored with cosine alone."""
    rng = np.random.default_rng(13)
    signs = rng.choice([-1.0, 1.0], size=24)
    scalars = {f"s{i}": np.array([signs[i]]) for i in range(24)}
    pairs = tuple(gold.GoldPair(f"s{i}", f"s{i + 1}", True, True) for i in range(0, 12, 2)) + tuple(
        gold.GoldPair(f"s{i}", f"s{i + 12}", False, False) for i in range(0, 12, 2)
    )

    cosines = {
        round(separability._similarity(scalars, pair.a, pair.b, "cosine"), 9) for pair in pairs
    }
    assert cosines <= {1.0, -1.0}

    vectors = {f"v{i}": _unit(rng.normal(size=2)) for i in range(20)}
    spread = {
        round(separability._similarity(vectors, f"v{i}", f"v{i + 10}", "cosine"), 9)
        for i in range(10)
    }
    assert len(spread) > 2


def test_whitening_amplifies_the_lowest_variance_directions() -> None:
    """The mechanism behind EXP-2.5-03's failure."""
    rng = np.random.default_rng(14)
    rows = rng.normal(size=(60, 5))
    rows[:, 4] *= 0.01
    centred = rows - rows.mean(axis=0)

    root = projection._inv_sqrt(np.cov(centred, rowvar=False))

    before = np.sqrt(np.var(centred, axis=0))
    whitened = centred @ root
    after = np.sqrt((whitened**2).sum(axis=0) / len(whitened))
    assert after[4] / after[0] > before[4] / before[0]
    assert float(np.max(after) / np.min(after)) < 1.5
