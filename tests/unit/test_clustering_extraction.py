"""Tests for the engine's task-string clustering.

The behaviour these pin down came out of EXP-2.5-08, which ran the shipped
sentence-embedding configuration over 1200 labelled strings and found 533 clusters for 48
object classes. Extract first and the same method gives 47 clusters with every class
intact. So the extraction is the load-bearing part, and the tests here are about it:

* an axis that is not asked for is not removed, because removing too much merges
  unrelated tasks and a merge is much harder to notice than a split;
* a word list that does not recognise a verb leaves the verb in the core rather than
  silently deleting the wrong words;
* a confirmed cluster stops moving, which is the promise the whole feature rests on.
"""

from __future__ import annotations

import numpy as np
import pytest

from data_engine.clustering import (
    Ignored,
    Lexicon,
    build,
    coverage,
    extract,
    extract_all,
    proposal_key,
    sample_tasks,
    token_vector,
)
from data_engine.clustering.online import OnlineCentroids, cosine

# ------------------------------------------------------------------ extraction


def test_the_core_drops_the_verb_and_the_colour_when_both_are_ignored() -> None:
    result = extract("pick up the red cube", ignored=Ignored(verb=True, colour=True))
    assert result.core == "cube"
    assert result.verb == "pick up"
    assert result.colours == ("red",)
    assert result.verb_matched is True
    assert result.single_token is True


def test_a_kept_axis_is_kept() -> None:
    """Removing an axis nobody asked to remove is a merge waiting to happen."""
    kept = extract("pick up the red cube", ignored=Ignored(verb=True, colour=False))
    assert kept.core == "red cube"
    assert kept.colours == ()
    kept_verb = extract("pick up the red cube", ignored=Ignored(verb=False, colour=True))
    assert kept_verb.core == "pick up cube"
    # The verb is still *recorded*: coverage is a fact about the lexicon, not about
    # which axes this run chose to remove.
    assert kept_verb.verb == "pick up"


def test_the_longest_verb_phrase_wins() -> None:
    """Matching "pick" alone would leave "up" behind, in the core, splitting one task."""
    assert extract("pick up the cube", ignored=Ignored()).verb == "pick up"
    assert extract("put down the bowl", ignored=Ignored()).verb == "put down"


def test_an_unrecognised_verb_leaves_the_core_rather_than_guessing() -> None:
    result = extract("tidy the shelf", ignored=Ignored(verb=True, colour=True))
    assert result.verb == ""
    assert result.verb_matched is False
    assert result.core == "tidy shelf"


def test_ignoring_locations_drops_the_destination_phrase() -> None:
    kept = extract("place the bowl on the plate", ignored=Ignored(verb=True, colour=True))
    dropped = extract(
        "place the bowl on the plate", ignored=Ignored(verb=True, colour=True, site=True)
    )
    assert kept.core == "bowl on plate"
    assert dropped.core == "bowl"


def test_a_string_with_no_determiner_still_reduces() -> None:
    """Real LeRobot sentences do not always have one; the core cannot depend on it."""
    result = extract("pink lego brick into the transparent box", ignored=Ignored(site=True))
    assert "lego brick" in result.core
    assert "box" not in result.core


def test_an_empty_string_yields_an_unusable_extraction() -> None:
    """It must never become a cluster of its own."""
    for text in ("", "   ", "!!!"):
        assert extract(text).usable is False
        assert extract(text).core == ""


def test_punctuation_and_case_do_not_change_the_core() -> None:
    assert extract("Pick up the RED cube!").core == extract("pick up the red cube").core


def test_extraction_is_deterministic() -> None:
    texts = ["pick up the red cube", "wipe the counter", "put down the bowl"]
    assert [item.core for item in extract_all(texts)] == [item.core for item in extract_all(texts)]


def test_a_lexicon_override_only_needs_the_key_it_changes() -> None:
    """An operator who finds a missing verb should not restate 300 words."""
    lexicon = Lexicon.from_json({"verbs": ["tidy"]})
    assert extract("tidy the shelf", lexicon, ignored=Ignored()).verb == "tidy"
    assert "red" in lexicon.colours


def test_ignored_parses_a_comma_list_and_describes_itself() -> None:
    assert Ignored.parse("verb,colour").names() == ["verb", "colour"]
    assert Ignored.parse("").names() == []
    assert Ignored.parse(None).describe() == "nothing"
    assert Ignored.parse("colour").describe() == "colour"
    # An unknown axis is ignored rather than raising: a typo should not 500 the page.
    assert Ignored.parse("colour,nonsense").names() == ["colour"]


def test_the_field_default_is_not_what_an_empty_checkbox_group_means() -> None:
    """The two ways of saying "no axes" disagree, and the disagreement is deliberate.

    `Ignored()` drops the verb and the colour, which is the only configuration that
    produced real groups in the scale experiment. `parse("")` means *nothing* is
    ignored, because that is what an unchecked Clusters form says. Reading the second as
    the first would report fragmentation the operator never asked for; reading the first
    as the second would never produce a group at all.
    """
    assert Ignored().names() == ["verb", "colour"]
    assert Ignored.parse("").names() == []
    assert Ignored.parse(" verb , colour ").names() == ["verb", "colour"]


# -------------------------------------------------------------------- coverage


def test_coverage_counts_unmatched_verbs_as_misses() -> None:
    items = extract_all(["pick up the cube", "tidy the shelf"], ignored=Ignored())
    measured = coverage(items)
    assert measured.strings == 2
    assert measured.verb_matched == 1
    assert measured.verb_rate == pytest.approx(0.5)
    assert measured.distinct_cores == 2


def test_coverage_of_nothing_is_zero_rather_than_an_error() -> None:
    measured = coverage([])
    assert measured.verb_rate == 0.0
    assert measured.as_dict()["strings"] == 0


# --------------------------------------------------------------------- vectors


def test_token_vectors_are_unit_length_and_stable() -> None:
    vector = token_vector("mug")
    assert len(vector) == 256
    assert np.linalg.norm(vector) == pytest.approx(1.0)
    assert vector == token_vector("mug")


def test_two_strings_sharing_every_token_are_identical() -> None:
    assert cosine(token_vector("red mug"), token_vector("red mug")) == pytest.approx(1.0)


def test_unrelated_strings_are_further_apart_than_identical_ones() -> None:
    same = cosine(token_vector("red mug"), token_vector("red mug"))
    near = cosine(token_vector("red mug"), token_vector("big red mug"))
    far = cosine(token_vector("red mug"), token_vector("laptop"))
    assert same > near > far


# ---------------------------------------------------------------------- online


def test_an_unknown_rule_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(ValueError, match="unknown centroid rule"):
        OnlineCentroids(rule="telepathy")


def test_an_impossible_radius_is_refused() -> None:
    with pytest.raises(ValueError, match="radius"):
        OnlineCentroids(radius=3.0)


def test_the_first_core_starts_a_cluster_and_the_next_joins_it() -> None:
    model = OnlineCentroids(radius=0.3)
    first = model.observe("mug")
    second = model.observe("mug")
    assert first.spawned is True
    assert second.spawned is False
    assert second.cluster == first.cluster
    assert len(model.clusters) == 1
    assert model.singletons == 0
    assert model.dominance == pytest.approx(1.0)


def test_the_cluster_cap_absorbs_rather_than_growing() -> None:
    model = OnlineCentroids(radius=0.0, max_clusters=2)
    for core in ("mug", "laptop", "drawer", "spoon"):
        model.observe(core)
    assert len(model.clusters) == 2


def test_a_frozen_cluster_stops_moving_and_still_takes_members() -> None:
    model = OnlineCentroids(radius=0.5)
    model.observe("mug")
    model.confirm(0, "mug handling")
    held = list(model.clusters[0].centroid)
    assignment = model.observe("mug with lid")
    assert assignment.cluster == 0
    assert assignment.into_frozen is True
    assert model.clusters[0].centroid == held
    assert model.clusters[0].size == 2


def test_confirming_an_unknown_cluster_raises_rather_than_creating_one() -> None:
    with pytest.raises(IndexError):
        OnlineCentroids().confirm(3, "nope")


def test_an_unfrozen_cluster_keeps_moving() -> None:
    model = OnlineCentroids(radius=0.5)
    model.observe("mug")
    before = list(model.clusters[0].centroid)
    model.observe("mug with lid")
    assert model.clusters[0].centroid != before


# ------------------------------------------------------------------ proposals


def _tasks() -> dict[str, int]:
    return {
        "pick up the red mug": 12,
        "pick up the blue mug": 3,
        "grab the mug": 5,
        "put down the laptop": 7,
        "open the drawer": 4,
    }


def test_grouping_cores_puts_the_three_mug_tasks_together() -> None:
    proposals = build(_tasks(), ignored=Ignored(verb=True, colour=True))
    cores = {proposal.core: proposal.size for proposal in proposals.proposals}
    assert cores["mug"] == 20
    assert cores["laptop"] == 7
    assert cores["drawer"] == 4
    assert proposals.cluster_count == 3


def test_health_counts_singletons_merges_and_dominance() -> None:
    proposals = build(_tasks(), ignored=Ignored(verb=True, colour=True))
    health = proposals.health()
    assert health["tasks"] == 5
    assert health["episodes"] == 31
    assert health["clusters"] == 3
    assert health["singletons"] == 2
    assert health["merges"] == 0
    assert health["dominance"] == pytest.approx(20 / 31, rel=1e-3)
    assert health["ignored"] == "verb, colour"


def test_proposals_are_ordered_by_episodes_so_the_big_ones_come_first() -> None:
    proposals = build(_tasks(), ignored=Ignored(verb=True, colour=True))
    sizes = [proposal.size for proposal in proposals.ordered()]
    assert sizes == sorted(sizes, reverse=True)


def test_applying_confirmations_freezes_by_key() -> None:
    proposals = build(_tasks(), ignored=Ignored(verb=True, colour=True))
    target = max(proposals.proposals, key=lambda item: item.size)
    applied = proposals.apply_confirmations({target.key: "mug handling"})
    assert applied == 1
    frozen = [item for item in proposals.proposals if item.frozen]
    assert len(frozen) == 1
    assert frozen[0].label == "mug handling"
    assert proposals.frozen == 1
    assert proposals.health()["frozen"] == 1


def test_applying_an_unknown_key_freezes_nothing() -> None:
    proposals = build(_tasks(), ignored=Ignored(verb=True, colour=True))
    assert proposals.apply_confirmations({"deadbeef": "nothing"}) == 0
    assert proposals.frozen == 0


def test_a_proposal_key_is_stable_and_content_addressed() -> None:
    assert proposal_key("mug") == proposal_key("mug")
    assert proposal_key("mug") != proposal_key("laptop")
    assert len(proposal_key("mug")) == 16


def test_building_with_an_explicit_order_is_reproducible() -> None:
    tasks = _tasks()
    first = build(tasks, ignored=Ignored(verb=True, colour=True), order=list(tasks))
    second = build(tasks, ignored=Ignored(verb=True, colour=True), order=list(tasks))
    assert [item.key for item in first.proposals] == [item.key for item in second.proposals]


def test_an_empty_catalog_proposes_nothing_rather_than_failing() -> None:
    proposals = build({}, ignored=Ignored(verb=True, colour=True))
    assert proposals.proposals == ()
    assert proposals.health()["clusters"] == 0
    assert proposals.dominance == 0.0


def test_the_sample_corpus_is_a_copy_so_a_caller_cannot_mutate_it() -> None:
    tasks = sample_tasks()
    tasks["pick up the red cube"] = 1
    assert "pick up the red cube" not in sample_tasks()
    assert len(sample_tasks()) > 20
