"""Unit tests for the pure task-vocabulary helpers (ADR 0029)."""

from __future__ import annotations

from data_engine.catalog.vocabulary import core_of, entry_id_for


def test_entry_ids_are_deterministic_and_label_derived() -> None:
    assert entry_id_for("Stack blocks") == entry_id_for("Stack blocks")
    assert entry_id_for("Stack blocks") == entry_id_for("  Stack blocks  ")
    assert entry_id_for("Stack blocks") != entry_id_for("stack blocks")
    assert entry_id_for("Stack blocks").startswith("voc_")


def test_the_core_is_the_measured_grouping_key() -> None:
    # Verb phrase and colour are stripped; the object and its site remain -
    # exactly what the measured grouping decided on (EXP-2.5-08). The lexicon's
    # full behaviour lives in the clustering tests.
    assert core_of("put the red mug on the plate") == "mug on plate"
    assert core_of("grab the blue mug on the plate") == "mug on plate"
