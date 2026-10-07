"""Unit tests for the synonym-candidate ranker (ADR 0029 §5)."""

from __future__ import annotations

from data_engine.clustering.ranker import candidates


def _entry(entry_id: str, label: str, core: str) -> dict[str, object]:
    return {"id": entry_id, "preferred_label": label, "core": core}


def _unmapped(task: str, episodes: int) -> dict[str, object]:
    return {"task_string": task, "episodes": episodes}


class TestRanker:
    def test_a_shared_core_attaches_to_the_one_entry_that_has_it(self) -> None:
        ranked = candidates(
            [_unmapped("pick up the red mug", 3), _unmapped("grab the blue mug", 1)],
            [_entry("voc_a", "mugs", "mug"), _entry("voc_b", "plates", "plate")],
        )

        assert len(ranked) == 1
        suggestion = ranked[0]
        assert suggestion.kind == "attach"
        assert suggestion.entry_id == "voc_a"
        assert suggestion.task_strings == ("grab the blue mug", "pick up the red mug")
        assert suggestion.episodes == 4

    def test_a_core_with_no_entry_proposes_a_new_entry_labelled_by_the_core(self) -> None:
        ranked = candidates([_unmapped("pick up the red mug", 2)], [])

        assert len(ranked) == 1
        assert ranked[0].kind == "new_entry"
        assert ranked[0].core == "mug"
        assert ranked[0].entry_id is None

    def test_an_ambiguous_core_is_never_suggested(self) -> None:
        ranked = candidates(
            [_unmapped("pick up the red mug", 5)],
            [_entry("voc_a", "mugs", "mug"), _entry("voc_b", "golden mug", "mug")],
        )

        assert ranked == [], "choosing between equal targets is a human's call"

    def test_suggestions_rank_by_fragmentation(self) -> None:
        ranked = candidates(
            [_unmapped("pick up the red mug", 2), _unmapped("open the drawer", 9)],
            [],
        )

        assert [c.core for c in ranked] == ["drawer", "mug"]

    def test_the_same_inputs_always_produce_the_same_queue(self) -> None:
        rows = [_unmapped("pick up the red mug", 2), _unmapped("pick up the blue mug", 2)]
        first = candidates(rows, [])
        second = candidates(list(reversed(rows)), [])

        assert first == second

    def test_a_string_with_no_surviving_core_proposes_nothing(self) -> None:
        assert candidates([_unmapped("the the the", 4)], []) == []

    def test_the_suggestion_serializes_for_the_api(self) -> None:
        ranked = candidates([_unmapped("pick up the red mug", 2)], [])

        assert ranked[0].as_dict() == {
            "kind": "new_entry",
            "core": "mug",
            "task_strings": ["pick up the red mug"],
            "episodes": 2,
            "entry_id": None,
            "entry_label": None,
        }
