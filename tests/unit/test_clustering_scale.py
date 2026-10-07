"""Tests for the full-scale harness behind `just cluster scale`."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from experiments.clustering import corpus, scale

from tests.unit._word_axes import WordAxes

CONFIG = scale.Config(
    encoder="word_axes", radius=0.9, colour_weight=0.0, mask_verbs=False, rule="running_mean"
)


def _run_with(labels: list[int], gold: list[str]) -> dict[str, float]:
    """A finished run described directly, so a metric is tested without a stream."""
    run = scale.Run(
        name="t",
        config=CONFIG,
        n=len(labels),
        order="shuffled",
        clusters=len(set(labels)),
        singletons=0,
        largest=1,
        cohesion=0.0,
        centroid_updates=0,
        arrival_labels=labels,
    )
    return run.quality(gold)


def _items(*pairs: tuple[str, str]) -> tuple[corpus.Item, ...]:
    return tuple(corpus.Item(text=text, label=label) for text, label in pairs)


def _matrix(items: tuple[corpus.Item, ...]) -> np.ndarray:
    return WordAxes().encode([item.text for item in items])


def test_a_stream_records_the_assignment_made_on_arrival() -> None:
    """Not a re-derivation from the final centroids, which can move a point's label."""
    items = _items(("pick up the cube", "cube"), ("pick up the cube", "cube"), ("wipe", "counter"))
    run = scale.stream(items, _matrix(items), CONFIG, name="t", order="shuffled")
    assert run.arrival_labels[:2] == [0, 0]
    assert run.arrival_labels[2] == 1
    assert run.clusters == 2
    assert run.singletons == 1
    assert run.dominance == pytest.approx(2 / 3)


def test_an_identical_string_always_joins_its_first_cluster() -> None:
    items = _items(*[("pick up the cube", "cube")] * 5)
    run = scale.stream(items, _matrix(items), CONFIG, name="t", order="shuffled")
    assert run.clusters == 1
    assert run.singletons == 0


def test_the_cluster_cap_is_reported_rather_than_disguised_as_a_count() -> None:
    config = scale.Config(
        encoder="word_axes", radius=0.01, colour_weight=0.0, rule="running_mean", max_clusters=3
    )
    items = _items(("alpha one", "a"), ("beta two", "b"), ("gamma three", "c"), ("delta four", "d"))
    run = scale.stream(items, _matrix(items), config, name="t", order="shuffled")
    assert run.clusters == 3
    assert run.capped is True
    assert run.n == 4


def test_a_run_that_never_hits_the_cap_says_so() -> None:
    items = _items(("alpha one", "a"), ("alpha one", "a"))
    assert scale.stream(items, _matrix(items), CONFIG, name="t", order="shuffled").capped is False


def test_fragmentation_counts_clusters_per_class_not_clusters() -> None:
    """One class in three clusters is 3.0; three classes in one cluster is 1.0."""
    assert _run_with([0, 1, 2], ["x", "x", "x"])["fragmentation"] == pytest.approx(3.0)
    assert _run_with([0, 0, 0], ["x", "y", "z"])["fragmentation"] == pytest.approx(1.0)


def test_a_class_split_across_clusters_is_counted_and_named() -> None:
    quality = _run_with([0, 0, 1], ["x", "y", "x"])
    assert quality["fragmentation"] == pytest.approx(1.5)
    assert quality["intact_labels"] == 1
    assert quality["impure_clusters"] == 1


def test_a_cluster_holding_two_classes_is_counted_as_impure() -> None:
    run = scale.Run(
        name="t",
        config=CONFIG,
        n=4,
        order="shuffled",
        clusters=2,
        singletons=0,
        largest=2,
        cohesion=0.1,
        centroid_updates=0,
        arrival_labels=[0, 0, 1, 1],
    )
    quality = run.quality(["a", "b", "a", "b"])
    assert quality["impure_clusters"] == 2
    assert quality["b_cubed"] < 1.0


def test_quality_of_an_unlabelled_corpus_is_empty_rather_than_wrong() -> None:
    run = scale.Run(
        name="t",
        config=CONFIG,
        n=2,
        order="shuffled",
        clusters=1,
        singletons=0,
        largest=2,
        cohesion=0.1,
        centroid_updates=0,
        arrival_labels=[0, 0],
    )
    assert run.quality([]) == {}


def test_scoring_a_subset_ignores_the_rows_from_other_origins() -> None:
    items = _items(("alpha", "x"), ("beta", "y"))
    items_with_origin = tuple(
        corpus.Item(text=item.text, label=item.label, origin="real") for item in items
    )
    run = scale.stream(items_with_origin, _matrix(items), CONFIG, name="t", order="shuffled")
    assert scale.score_subset(run, items_with_origin, "synthetic") == {}
    assert scale.score_subset(run, items_with_origin, "real")["b_cubed"] == pytest.approx(1.0)


def test_subset_quality_restricts_to_one_attribute_value() -> None:
    items = (
        corpus.Item(text="pick up the red cube", label="cube", colour="red"),
        corpus.Item(text="pick up the blue cube", label="cube", colour="blue"),
        corpus.Item(text="pick up the cube", label="cube"),
    )
    run = scale.stream(items, _matrix(items), CONFIG, name="t", order="shuffled")
    uncoloured = scale.subset_quality(run, items, "colour", "none")
    assert uncoloured["strings"] == 1
    assert scale.subset_quality(run, items, "colour", "green") == {}


def test_fragmentation_by_attribute_reports_where_the_split_happened() -> None:
    items = _items(("alpha", "x"), ("beta", "y"))
    buckets = scale.fragmentation_by_attribute(_run(items), items, "colour")
    assert set(buckets) == {"none"}
    assert buckets["none"]["strings"] == 2.0
    assert buckets["none"]["clusters"] == 2.0


def _run(items: tuple[corpus.Item, ...]) -> scale.Run:
    return scale.stream(items, _matrix(items), CONFIG, name="t", order="shuffled")


def test_encoding_returns_unit_rows_and_a_timing() -> None:
    items = corpus.synthetic_corpus(120)
    matrix, seconds = scale.encode(items, WordAxes(), CONFIG)
    assert matrix.shape == (120, WordAxes.dim)
    norms = np.linalg.norm(matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)
    assert seconds >= 0.0


def test_masking_verbs_changes_the_matrix_it_produces() -> None:
    """Otherwise a sweep that claims to compare masking is comparing nothing."""
    items = _items(("pick up the cube", "cube"), ("lift the cube", "cube"))
    base = replace(CONFIG, colour_weight=1.0)
    masked, _ = scale.encode(items, WordAxes(), replace(base, mask_verbs=True))
    kept, _ = scale.encode(items, WordAxes(), replace(base, mask_verbs=False))
    assert masked.shape == kept.shape
    assert np.allclose(masked[0], masked[1]), "masking makes the two verbs one input"
    assert not np.allclose(kept[0], kept[1])


def test_the_scale_curve_only_reports_sizes_the_corpus_can_reach() -> None:
    items = corpus.synthetic_corpus(300)
    vectors, _ = scale.encode(items, WordAxes(), CONFIG)
    curve = scale.scale_curve(corpus.shuffled(items), vectors, CONFIG, sizes=(100, 300, 900))
    assert [run.n for run in curve] == [100, 300]


def test_order_sensitivity_compares_two_orderings_of_the_same_strings() -> None:
    items = corpus.synthetic_corpus(200)
    vectors, _ = scale.encode(items, WordAxes(), CONFIG)
    stats = scale.order_sensitivity(corpus.shuffled(items), vectors, CONFIG)
    assert -1.0 <= stats["adjusted_rand"] <= 1.0
    assert stats["clusters_shuffled"] > 0


def test_batch_sensitivity_reports_a_spread_not_a_verdict() -> None:
    items = corpus.synthetic_corpus(150)
    stats = scale.batch_sensitivity(items, WordAxes(), CONFIG, runs=2)
    assert stats["spread"] >= 0
    assert len(stats["clusters"]) == 2


def test_verb_coverage_counts_unrecognised_verbs_as_misses() -> None:
    """`verb_of` answers "-" for a miss, and "-" is a truthy string."""
    items = (
        corpus.Item(text="tidy the shelf", label="shelf", verb="tidy", unknown_verb=True),
        corpus.Item(text="pick up the cube", label="cube", verb="pick up"),
    )
    note = scale.coverage_note(items)
    assert note["out_of_lexicon_verbs"] == 1
    assert note["coverage"] == pytest.approx(0.5)


def test_extracting_the_core_beats_embedding_the_sentence_by_a_wide_margin() -> None:
    """The headroom claim, stated as a test."""
    config = replace(CONFIG, radius=0.3)
    items = corpus.synthetic_corpus(240)
    gold = [item.label for item in items]
    sentences = scale.stream(
        corpus.shuffled(items),
        scale.encode(items, WordAxes(), config)[0],
        config,
        name="sentences",
        order="shuffled",
    ).quality(gold)
    core = scale.core_run(items, WordAxes(), config).quality(gold)
    assert core["b_cubed"] > sentences["b_cubed"] + 0.3
    assert core["fragmentation"] < sentences["fragmentation"]
    assert core["impure_clusters"] <= sentences["impure_clusters"]
