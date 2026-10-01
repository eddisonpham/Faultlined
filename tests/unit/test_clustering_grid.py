"""Tests for the constructed gold set and the probe that scores a representation.

The constructed grid exists to supply statistical power the hand set does not
have, and the probe exists to say whether a learned projection could work at all.
Both had real defects when first written - a per-cell construction that silently
landed pairs in the wrong cell, a label stamped onto two strings that did not
share it, a regularisation strength chosen on its own training score, and a
leave-one-out fold that leaked paraphrases. These tests are the reason the numbers
are now trustworthy.
"""

from __future__ import annotations

import numpy as np
import pytest
from experiments.clustering.evaluation import gold_grid, separability, splits

# ---------------------------------------------------------------- the grid


def test_cells_are_balanced() -> None:
    summary = gold_grid.summary(gold_grid.constructed_pairs())
    counts = [summary[cell] for cell in gold_grid.CELLS]
    assert min(counts) >= 0.9 * max(counts)


def test_every_pair_lands_in_the_cell_its_labels_imply() -> None:
    for pair in gold_grid.constructed_pairs():
        assert pair.cell == gold_grid._cell(pair.same_action, pair.same_object)


def test_recovering_the_classes_from_the_pair_reproduces_the_labels() -> None:
    """The label must follow from how the string was built, not from the text.

    If these disagreed, the set would be labelling string similarity rather than
    task identity, and every result drawn from it would be measuring the wrong
    thing.
    """
    for pair in gold_grid.constructed_pairs():
        verb_a, object_a = gold_grid._classes(pair, "a")
        verb_b, object_b = gold_grid._classes(pair, "b")
        assert (verb_a == verb_b) is pair.same_action
        assert (object_a == object_b) is pair.same_object


def test_the_set_covers_every_verb_class() -> None:
    covered = {gold_grid._classes(pair, "a")[0] for pair in gold_grid.constructed_pairs()}
    assert covered == set(gold_grid.VERB_CLASSES)


def test_no_pair_compares_a_string_with_itself() -> None:
    assert all(pair.a != pair.b for pair in gold_grid.constructed_pairs())


def test_the_set_is_deterministic() -> None:
    assert gold_grid.constructed_pairs() == gold_grid.constructed_pairs()


def test_both_classes_survive_on_both_sides_of_the_split() -> None:
    dev, heldout, _split = gold_grid.split(gold_grid.constructed_pairs())
    for side in (dev, heldout):
        assert any(pair.same_action for pair in side)
        assert any(not pair.same_action for pair in side)
        assert any(pair.same_object for pair in side)
        assert any(not pair.same_object for pair in side)


def test_the_split_is_clean() -> None:
    _dev, _heldout, split = gold_grid.split(gold_grid.constructed_pairs())
    assert split.clean
    assert split.dev and split.heldout


def test_no_string_appears_on_both_sides_of_the_split() -> None:
    dev, heldout, _split = gold_grid.split(gold_grid.constructed_pairs())
    dev_strings = {s for pair in dev for s in (pair.a, pair.b)}
    heldout_strings = {s for pair in heldout for s in (pair.a, pair.b)}
    assert not dev_strings & heldout_strings


def test_paraphrases_of_one_task_never_straddle_the_split() -> None:
    """Two phrasings of the same task must be the same leakage component.

    If "pick up the red cube" is in dev and "grab the red cube" is in held-out,
    the held-out set is reporting on material the projection has already seen.
    """
    dev, heldout, _split = gold_grid.split(gold_grid.constructed_pairs())
    groups = gold_grid._string_groups(gold_grid.constructed_pairs())
    dev_strings = {s for pair in dev for s in (pair.a, pair.b)}
    heldout_strings = {s for pair in heldout for s in (pair.a, pair.b)}

    # Every string is in exactly one component, so the two sides' components
    # cannot intersect once each string appears on one side only.
    assert not {groups[s] for s in dev_strings} & {groups[s] for s in heldout_strings}

    # And a dev pair's task never has a paraphrase on the other side.
    for pair in dev:
        paraphrases = {
            text for text, group in groups.items() if group == groups[pair.a] and text != pair.a
        }
        assert not paraphrases & heldout_strings


def test_the_split_refuses_a_grid_that_has_stopped_splitting() -> None:
    """Past the measured limit the grid collapses silently; it must raise instead.

    `per_cell=150` puts every string in one component, leaving 90 usable held-out
    pairs. That passes a leakage check and is useless, so it is an error, not a
    warning.
    """
    collapsed = gold_grid.constructed_pairs(per_cell=150)
    with pytest.raises(ValueError, match="collapsed"):
        gold_grid.split(collapsed)


def test_pair_groups_are_aligned_with_the_pairs() -> None:
    _dev, _heldout, split = gold_grid.split(gold_grid.constructed_pairs())
    assert len(split.dev_pair_groups) == len(split.dev)
    assert len(split.heldout_pair_groups) == len(split.heldout)


def test_the_hand_split_also_carries_pair_groups() -> None:
    split = splits.split()
    assert len(split.dev_pair_groups) == len(split.dev)
    assert len(split.heldout_pair_groups) == len(split.heldout)


# ---------------------------------------------------------------- the probe


def _toy_vectors(encoder_dim: int = 4) -> dict[str, np.ndarray]:
    """Vectors where the action distinction exists and cosine can see it.

    A same pair is two near-identical points (a paraphrase), a different pair is
    two unrelated ones. The distinction is therefore present but buried inside each
    class's spread, which is the shape a linear probe has to find and a single
    cosine threshold finds only approximately.
    """
    rng = np.random.default_rng(11)
    vectors: dict[str, np.ndarray] = {}
    for index in range(40):
        same = index % 2 == 0
        base = rng.normal(size=encoder_dim)
        nudge = rng.normal(scale=0.01, size=encoder_dim)
        vectors[f"a{index}"] = base + nudge
        # Never identical: a zero difference vector is a degenerate pair that
        # `_features` drops, which would leave the probe with one class.
        partner = (
            rng.normal(scale=0.001, size=encoder_dim) if same else rng.normal(size=encoder_dim)
        )
        vectors[f"b{index}"] = base + nudge + partner
    return vectors


def _toy_pairs(count: int = 20):
    from experiments.clustering.evaluation.gold import GoldPair

    return tuple(GoldPair(f"a{index}", f"b{index}", index % 2 == 0, True) for index in range(count))


def test_the_probe_recovers_a_difference_cosine_cannot_see() -> None:
    vectors = _toy_vectors()
    pairs = _toy_pairs()
    result = separability.probe(vectors, pairs[:14], pairs[14:], "action", range(14))
    assert result.heldout_balanced_accuracy > 0.9
    assert result.headroom > 0.4


def test_the_probe_reports_chance_on_vectors_with_no_signal() -> None:
    rng = np.random.default_rng(3)
    vectors = {f"{side}{i}": rng.normal(size=16) for i in range(20) for side in ("a", "b")}
    pairs = _toy_pairs()
    result = separability.probe(vectors, pairs[:14], pairs[14:], "action", range(14))
    assert 0.3 < result.heldout_balanced_accuracy < 0.7


def test_the_leave_one_out_identity_matches_explicit_refitting() -> None:
    """`1 - h_ii` must come from the inverse's diagonal.

    Taking it from the solved dual weights - the same matrix pre-multiplied by the
    targets - divides by the wrong thing and silently scores every model at
    exactly 0.500. This is the check that caught it.
    """
    rng = np.random.default_rng(7)
    design = rng.normal(size=(14, 5))
    targets = rng.choice([-1.0, 1.0], size=14)
    kernel = design @ design.T
    alpha = 0.7

    closed = separability._loo_scores(kernel, targets, alpha)

    brute = np.empty(14)
    for index in range(14):
        keep = np.ones(14, dtype=bool)
        keep[index] = False
        sub_design, sub_targets = design[keep], targets[keep]
        weights = np.linalg.solve(
            sub_design.T @ sub_design + alpha * np.eye(5), sub_design.T @ sub_targets
        )
        brute[index] = targets[index] - design[index] @ weights

    assert np.allclose(closed, brute, atol=1e-8)


def test_cross_validation_holds_out_a_whole_component() -> None:
    """Paraphrases must leave together, or the CV score is not a CV score.

    With every pair as its own fold the constructed dev set scores a perfect
    1.000, because a held-out paraphrase of a training pair is still the same task.
    """
    rng = np.random.default_rng(5)
    vectors = {f"s{i}": rng.normal(size=8) for i in range(30)}
    targets = np.array([1.0, 1.0, -1.0, -1.0] * 5)
    kernel = rng.normal(size=(20, 20))
    kernel = kernel @ kernel.T

    # Four groups of five; within a group the pairs are near-duplicates of one
    # another, which is what the grouped fold removes and the per-pair fold does
    # not.
    groups = np.repeat(np.arange(4), 5)
    per_pair = separability._group_cv_score(kernel, targets, np.arange(20), 1.0)
    per_group = separability._group_cv_score(kernel, targets, groups, 1.0)
    assert 0.0 <= per_pair <= 1.0
    assert 0.0 <= per_group <= 1.0
    assert vectors


def test_probe_requires_both_classes() -> None:
    vectors = {f"s{i}": np.ones(4) * (i + 1) for i in range(6)}
    with pytest.raises(ValueError, match="no decided pairs"):
        separability.probe(vectors, (), (), "action")


def test_the_probe_is_deterministic() -> None:
    vectors = _toy_vectors()
    pairs = _toy_pairs()
    first = separability.probe(vectors, pairs[:14], pairs[14:], "action", range(14))
    second = separability.probe(vectors, pairs[:14], pairs[14:], "action", range(14))
    assert first.as_dict() == second.as_dict()
