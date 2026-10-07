"""Invariants of the stage 2.5 gold set and its leakage-safe split."""

from __future__ import annotations

import pytest
from experiments.clustering.evaluation import gold, splits


@pytest.mark.unit
def test_the_gold_set_has_fifty_pairs() -> None:
    """The size was agreed with the owner; a silent shrink would be a quiet change."""
    assert len(gold.GOLD_PAIRS) == 50


@pytest.mark.unit
def test_every_pair_labels_both_views() -> None:
    """A missing view label would silently drop the pair from that view's metrics."""
    for pair in gold.GOLD_PAIRS:
        assert pair.same_action in (True, False, None), pair
        assert pair.same_object in (True, False, None), pair
        assert pair.ambiguous == (pair.same_action is None or pair.same_object is None)


@pytest.mark.unit
def test_pairs_are_distinct_and_never_self_comparative() -> None:
    seen: set[tuple[str, str]] = set()
    for pair in gold.GOLD_PAIRS:
        assert pair.a != pair.b, pair
        key = tuple(sorted((pair.a, pair.b)))
        assert key not in seen, f"duplicate pair: {key}"
        seen.add(key)


@pytest.mark.unit
def test_ambiguous_pairs_justify_themselves() -> None:
    """An ambiguity flag with no stated reason is not reviewable by a human."""
    for pair in gold.GOLD_PAIRS:
        if pair.ambiguous:
            assert pair.note.strip(), f"ambiguous but unexplained: {pair}"


@pytest.mark.unit
def test_both_views_have_enough_decided_pairs_to_measure() -> None:
    summary = gold.summary()
    assert summary["action_decided"] >= 30, summary
    assert summary["object_decided"] >= 30, summary
    assert summary["ambiguous"] >= 5, summary


@pytest.mark.unit
def test_the_split_never_shares_a_leakage_component() -> None:
    """The core guarantee."""
    result = splits.split()
    assert result.clean, result.report()
    assert result.report()["shared_components"] == 0


@pytest.mark.unit
def test_the_split_keeps_both_halves_usable() -> None:
    report = splits.split().report()
    assert report["dev_pairs"] >= 20, report
    assert report["heldout_pairs"] >= 15, report


@pytest.mark.unit
def test_the_split_is_deterministic() -> None:
    """A split that moves between runs makes every comparison unrepeatable."""
    first, second = splits.split(), splits.split()
    assert first.dev == second.dev
    assert first.heldout == second.heldout


@pytest.mark.unit
def test_every_pair_lands_exactly_once() -> None:
    result = splits.split()
    assert set(result.dev).isdisjoint(result.heldout)
    assert len(result.dev) + len(result.heldout) == len(gold.GOLD_PAIRS)


@pytest.mark.unit
def test_both_strings_of_a_pair_stay_on_the_same_side() -> None:
    """Half a pair is meaningless, so a pair can never be split."""
    result = splits.split()
    groups = splits.component_map()
    dev_components = {groups[pair.a] for pair in result.dev} | {
        groups[pair.b] for pair in result.dev
    }
    for pair in result.heldout:
        assert groups[pair.a] not in dev_components
        assert groups[pair.b] not in dev_components


@pytest.mark.unit
def test_near_duplicate_strings_are_grouped_together() -> None:
    """The lexical edges are what stop a rephrasing from straddling the split."""
    groups = splits.component_map()
    assert groups["grab the red cube"] == groups["pick up the red cube"]
    assert groups["wipe the table"] == groups["wipe down the table"]


@pytest.mark.unit
def test_unrelated_strings_are_not_lexically_merged() -> None:
    """The lexical guard must not fire on short strings sharing function words."""
    unrelated = [
        ("open the door", "close the drawer"),
        ("fold the cloth", "pour the water"),
        ("pick up the red cube", "screw in the bolt"),
    ]
    for left, right in unrelated:
        overlap = splits.jaccard(left, right)
        assert overlap < splits.JACCARD_THRESHOLD, (left, right, overlap)


@pytest.mark.unit
def test_the_split_is_not_degenerate() -> None:
    """Both sides must carry decided pairs in both views, or a held-out score is meaningless."""
    result = splits.split()
    for side, pairs in (("dev", result.dev), ("heldout", result.heldout)):
        for view, label in (("action", "same_action"), ("object", "same_object")):
            decided = [p for p in pairs if getattr(p, label) is not None]
            assert decided, f"{side} has no decided {view} pairs"


@pytest.mark.unit
def test_positive_and_negative_pairs_survive_on_both_sides() -> None:
    """A split where one side is all-positives would flatter or bury a method."""
    result = splits.split()
    for side, pairs in (("dev", result.dev), ("heldout", result.heldout)):
        decided = [p for p in pairs if p.same_action is not None]
        assert any(p.same_action for p in decided), f"{side} has no action positives"
        assert any(not p.same_action for p in decided), f"{side} has no action negatives"


@pytest.mark.unit
def test_gold_for_a_view_excludes_that_views_ambiguities() -> None:
    for view, decided in (("action", "same_action"), ("object", "same_object")):
        pairs = gold.gold_for(view)
        assert pairs
        for pair in pairs:
            assert getattr(pair, decided) is not None
            assert len(pairs) <= len(gold.GOLD_PAIRS)
