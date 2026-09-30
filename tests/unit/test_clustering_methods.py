"""Unit tests for the clustering method, centroid rules, and metrics.

Deliberately built on the `null` encoder. These tests are about *plumbing and
invariants* - does the freeze actually freeze, does a metric compute the right
thing - and none of them should need a model downloaded to pass. A test that
requires a network fetch is a test that fails for reasons unrelated to the code.
"""

from __future__ import annotations

import numpy as np
import pytest
from experiments.clustering import embeddings
from experiments.clustering.centroids import Ema, Huber, Medoid, RobustTrim, RunningMean
from experiments.clustering.evaluation import metrics
from experiments.clustering.evaluation.gold import GoldPair
from experiments.clustering.methods.online_centroids import OnlineCentroids

UNIT = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.1, 0.9]], dtype=np.float32)
UNIT_NORM = UNIT / np.linalg.norm(UNIT, axis=1, keepdims=True)


@pytest.mark.unit
def test_the_null_encoder_is_deterministic_and_unit_norm() -> None:
    """Determinism is what lets an experiment result be compared to a later run."""
    first = embeddings.NullEncoder().encode(["pick up the red cube", "pour the water"])
    second = embeddings.NullEncoder().encode(["pick up the red cube", "pour the water"])
    assert np.array_equal(first, second)
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-5)


@pytest.mark.unit
def test_encode_all_rejects_a_wrong_shaped_result() -> None:
    class Broken:
        name, dim = "broken", 4

        def encode(self, texts: object) -> np.ndarray:
            return np.zeros((len(texts), 3), dtype=np.float32)  # type: ignore[arg-type]

        def fingerprint(self) -> str:
            return "broken"

    with pytest.raises(ValueError, match="expected"):
        embeddings.encode_all(Broken(), ["a", "b"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_null_encoder_is_never_reported_as_semantic() -> None:
    """Guards the control backend from being quoted as a method result."""
    assert not embeddings.is_semantic(embeddings.NullEncoder())
    assert not embeddings.describe(embeddings.NullEncoder())["semantic"]


@pytest.mark.unit
def test_an_unknown_backend_name_lists_the_known_ones() -> None:
    with pytest.raises(KeyError, match="known backends"):
        embeddings.get("no-such-backend")


@pytest.mark.unit
@pytest.mark.parametrize(
    "rule", [RunningMean(), Ema(0.9), RobustTrim(8, 0.25), Huber(8, 1.0), Medoid(8)]
)
def test_every_rule_returns_a_unit_vector(rule: object) -> None:
    centroid = np.array([1.0, 0.0], dtype=np.float32)
    for point in UNIT_NORM:
        centroid = rule.update(centroid, point)  # type: ignore[attr-defined]
        assert abs(float(np.linalg.norm(centroid)) - 1.0) < 1e-5, rule.name  # type: ignore[attr-defined]


@pytest.mark.unit
def test_a_rule_survives_a_freshly_spawned_cluster() -> None:
    """A brand-new cluster has one point. `RobustTrim` raised here on its first
    update because the requested kth exceeded the buffer length."""
    for rule in (RobustTrim(64, 0.2), Huber(64, 1.0), Medoid(32)):
        centroid = rule.update(
            np.array([1.0, 0.0], dtype=np.float32), np.array([0.0, 1.0], np.float32)
        )
        assert np.isfinite(centroid).all(), rule.name


@pytest.mark.unit
def test_robust_rules_resist_an_outlier_that_the_running_mean_follows() -> None:
    """The property that separates them, stated as a test rather than a claim.

    Both rules must be primed first: on the very first update every rule returns
    the point itself, because a one-member cluster's centre *is* that member.
    Comparing them on a cold start would compare nothing.
    """
    outlier = np.array([-1.0, 0.0], dtype=np.float32)
    start = np.array([0.98, 0.2], dtype=np.float32)
    start = start / np.linalg.norm(start)
    inliers = [start, np.array([0.95, 0.25], dtype=np.float32)]

    def pull(rule: object) -> float:
        centroid = start
        for point in inliers:
            centroid = rule.update(centroid, point)  # type: ignore[attr-defined]
        settled = centroid.copy()
        moved = rule.update(settled, outlier)  # type: ignore[attr-defined]
        return float(1.0 - settled @ moved)

    naive = pull(RunningMean())
    robust = pull(Medoid(8))
    assert robust < naive, (naive, robust)


@pytest.mark.unit
def test_ema_bounds_a_single_point_more_than_a_running_mean() -> None:
    """A slow EMA resists a single point more than a running mean does.

    Primed on inliers first: on a cold start every rule returns the point itself,
    because a one-member cluster's centre is that member, and the comparison
    would be between two identical answers.
    """
    # Deliberately non-collinear. An exactly anti-parallel outlier in one
    # dimension normalises straight back to the start direction, which makes the
    # comparison meaningless.
    start = np.array([1.0, 0.0], dtype=np.float32)
    inlier = np.array([0.92, 0.39], dtype=np.float32)
    outlier = np.array([0.0, 1.0], dtype=np.float32)

    def primed(rule: object) -> np.ndarray:
        centre = start
        for point in (start, inlier, inlier):
            centre = rule.update(centre, point)  # type: ignore[attr-defined]
        return centre

    mean = RunningMean()
    mean_alignment = float(start @ mean.update(primed(mean), outlier))
    slow = Ema(0.98)
    ema_alignment = float(start @ slow.update(primed(slow), outlier))

    assert ema_alignment > mean_alignment, (ema_alignment, mean_alignment)


@pytest.mark.unit
def test_a_confirmed_cluster_never_moves() -> None:
    """The guarantee that makes a human's label a promise rather than a guess."""
    model = OnlineCentroids(radius=0.5, rule_factory=RunningMean)
    model.fit(UNIT_NORM[:2], ["a", "b"])
    assert len(model.clusters) == 1
    before = model.clusters[0].centroid.copy()
    model.confirm(0, "pick up something")

    for point in UNIT_NORM[2:]:
        model.observe(point, "later")

    assert np.array_equal(model.clusters[0].centroid, before)
    assert model.clusters[0].frozen is True
    assert model.clusters[0].label == "pick up something"


@pytest.mark.unit
def test_a_point_within_radius_joins_the_nearest_cluster() -> None:
    model = OnlineCentroids(radius=0.5, rule_factory=RunningMean)
    model.fit(UNIT_NORM[:2], ["a", "b"])
    model.observe(UNIT_NORM[1], "near")
    assert len(model.clusters) == 1
    assert model.clusters[0].count == 3


@pytest.mark.unit
def test_a_far_point_spawns_a_new_cluster() -> None:
    model = OnlineCentroids(radius=0.1, rule_factory=RunningMean)
    model.observe(UNIT_NORM[0], "a")
    model.observe(UNIT_NORM[2], "b")
    assert len(model.clusters) == 2


@pytest.mark.unit
def test_the_cluster_cap_is_respected() -> None:
    model = OnlineCentroids(radius=0.01, max_clusters=2, rule_factory=RunningMean)
    for index, point in enumerate(UNIT_NORM):
        model.observe(point, str(index))
    assert len(model.clusters) == 2


@pytest.mark.unit
def test_a_rejected_radius_or_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        OnlineCentroids(radius=0.0)
    with pytest.raises(ValueError):
        OnlineCentroids(max_clusters=0)


@pytest.mark.unit
def test_the_result_flags_a_dominant_cluster() -> None:
    """One giant cluster plus singletons is a failure mode a score can hide."""
    model = OnlineCentroids(radius=1.99, rule_factory=RunningMean)
    result = model.fit(UNIT_NORM, ["a", "b", "c", "d"])
    assert result.dominance == 1.0
    assert result.cluster_count == 1


@pytest.mark.unit
def test_over_merge_rate_is_the_thing_pair_metrics_exist_to_catch() -> None:
    """A method that merges unrelated things scores well on ARI and is unusable."""
    labels = {"pick up the red cube": 0, "grab the red cube": 0, "screw in the bolt": 0}
    pairs = (
        GoldPair("pick up the red cube", "grab the red cube", True, True),
        GoldPair("pick up the red cube", "screw in the bolt", False, False),
    )
    scores = metrics.pair_scores(labels, pairs, "action")
    assert scores.over_merge_rate == 1.0
    assert scores.under_merge_rate == 0.0


@pytest.mark.unit
def test_ambiguous_pairs_are_counted_and_never_scored() -> None:
    labels = {"a": 0, "b": 0}
    pairs = (GoldPair("a", "b", None, True, "arguable"),)
    scores = metrics.pair_scores(labels, pairs, "action")
    assert scores.ambiguous_pairs == 1
    assert scores.scored_pairs == 0


@pytest.mark.unit
def test_unassigned_strings_are_reported() -> None:
    """A method that drops inputs must not read as a clean result."""
    labels = {"a": 0}
    pairs = (GoldPair("a", "b", True, True),)
    scores = metrics.pair_scores(labels, pairs, "action")
    assert scores.unassigned == ("b",)


@pytest.mark.unit
def test_ari_is_one_for_an_identical_partition_and_zero_for_chance() -> None:
    assert metrics.adjusted_rand([0, 0, 1, 1], [0, 0, 1, 1]) == pytest.approx(1.0)
    # Everything in one cluster is a degenerate partition: 0/0, reported as 0.
    assert metrics.adjusted_rand([0, 0, 0, 0], [0, 0, 0, 0]) == 0.0


@pytest.mark.unit
def test_b_cubed_rewards_a_singleton_agreement() -> None:
    """A singleton in both labellings is a perfect match, not a 0/0."""
    assert metrics.b_cubed([0, 1], [0, 1]) == pytest.approx(1.0)


@pytest.mark.unit
def test_ari_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        metrics.adjusted_rand([0, 1], [0, 1, 2])
