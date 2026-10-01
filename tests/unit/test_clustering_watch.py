"""Tests for the live clustering session behind `just cluster watch`.

The point of the live view is one property that no offline metric shows: a
cluster a person confirms stops moving while its neighbours keep reshaping. These
tests assert that property directly, and pin the rest of the session contract -
what an arrival reports, what the page is told, and what happens before the first
string arrives - so the demo cannot drift into lying about the state it shows.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zlib

import numpy as np
import pytest
from experiments.clustering import watch

#: Wide enough that the test vocabulary never collides on an axis.
DIM = 256


class _WordAxes:
    """A deterministic stand-in for a sentence encoder.

    Every distinct word gets its own axis with weight 1, so cosine similarity
    between two sentences is the fraction of words they share. Nothing about a
    model's training run can change the outcome here: what is under test is the
    method, not the encoder.
    """

    name = "word_axes"
    dim = DIM

    def encode(self, texts: object) -> np.ndarray:
        rows = []
        for text in texts:  # type: ignore[union-attr]
            vector = np.zeros(DIM, dtype=np.float32)
            for word in str(text).lower().split():
                vector[zlib.crc32(word.encode()) % DIM] += 1.0
            norm = float(np.linalg.norm(vector))
            rows.append(vector / norm if norm else vector)
        return np.stack(rows)

    def fingerprint(self) -> str:
        return "word-axes"


def _session(**options: object) -> watch.Session:
    defaults: dict[str, object] = {
        "encoder": _WordAxes(),
        "radius": 0.20,
        "colour_weight": 0.0,
        "mask_verbs": False,
        "rule": "sliding_8",
    }
    defaults.update(options)
    return watch.Session(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------ ingestion


def test_the_first_arrival_starts_a_cluster() -> None:
    session = _session()
    event = session.feed("pick up the red cube")
    assert event["outcome"] == "new cluster"
    assert event["cluster"] == 0
    assert len(session.model.clusters) == 1


def test_an_emptied_string_is_skipped_rather_than_encoded() -> None:
    """A blank line from a terminal must not become a cluster of its own."""
    session = _session()
    event = session.feed("   ")
    assert event["outcome"] == "skipped"
    assert session.texts == []
    assert len(session.model.clusters) == 0


def test_a_near_duplicate_joins_the_cluster_it_resembles() -> None:
    session = _session()
    session.feed("place the bowl on the plate")
    event = session.feed("place the bowl on the tray")
    assert event["outcome"] == "joined"
    assert event["cluster"] == 0
    assert len(session.model.clusters) == 1


def test_masking_verbs_collapses_two_verbs_onto_one_input() -> None:
    """The EXP-2.5-07 effect, on the configuration the demo actually runs.

    Without masking these two share two words out of three and sit outside the
    radius, so they split. With masking they are the same string and must not.
    """
    kept = _session()
    kept.feed("lift the bowl")
    kept.feed("place the bowl")
    assert len(kept.model.clusters) == 2

    masked = _session(mask_verbs=True)
    first = masked.feed("lift the bowl")
    second = masked.feed("place the bowl")
    assert first["masked_verb"] == "lift"
    assert second["masked_verb"] == "place"
    assert len(masked.model.clusters) == 1


def test_the_log_is_bounded_so_a_long_session_cannot_grow_without_limit() -> None:
    session = _session()
    session.feed_all(f"task number {index}" for index in range(watch.LOG_LIMIT + 20))
    assert len(session.arrivals) == watch.LOG_LIMIT


def test_feed_all_accepts_a_generator() -> None:
    """`--source` hands over a file iterator, not a list."""
    session = _session()
    session.feed_all(line for line in ["alpha one", "beta two"])
    assert len(session.texts) == 2


# ------------------------------------------------------------------- freezing


def test_confirming_a_cluster_freezes_it() -> None:
    session = _session()
    session.feed("pick up the cube")
    assert session.confirm(0, "picking") is True
    assert session.model.clusters[0].frozen is True
    assert session.model.clusters[0].label == "picking"


def test_a_frozen_centroid_does_not_move_when_more_members_arrive() -> None:
    """The demonstration this whole demo exists to make.

    Two later arrivals both belong to the same cluster and would normally drag its
    centroid. Confirmed, they join it and the centroid stays exactly where the
    person put it.
    """
    session = _session()
    session.feed("pick up the cube")
    session.confirm(0)
    frozen_centroid = session.model.clusters[0].centroid.copy()

    session.feed("pick up the big cube")
    session.feed("pick up the small cube")

    cluster = session.model.clusters[0]
    assert cluster.count == 3
    np.testing.assert_array_equal(cluster.centroid, frozen_centroid)


def test_arrivals_into_a_frozen_cluster_are_reported_as_such() -> None:
    session = _session()
    session.feed("pick up the cube")
    session.confirm(0)
    event = session.feed("pick up the big cube")
    assert event["outcome"] == "joined"
    assert event["into_frozen"] is True


def test_an_unfrozen_neighbour_keeps_moving_while_a_frozen_one_does_not() -> None:
    """Freezing one cluster must not freeze the session."""
    session = _session()
    session.feed("pick up the cube")
    session.feed("wipe the counter")
    session.confirm(0)

    held = session.model.clusters[0].centroid.copy()
    free_before = session.model.clusters[1].centroid.copy()
    session.feed("pick up the big cube")  # frozen cluster 0
    session.feed("wipe the long counter")  # cluster 1, still free

    np.testing.assert_array_equal(session.model.clusters[0].centroid, held)
    assert not np.array_equal(session.model.clusters[1].centroid, free_before)


def test_confirming_an_index_that_is_not_a_cluster_is_refused() -> None:
    session = _session()
    assert session.confirm(0) is False
    assert session.model.clusters == []
    session.feed("pick up the cube")
    assert session.confirm(7) is False


def test_freezing_does_not_widen_the_radius() -> None:
    """A confirmed cluster still refuses points far outside it; those spawn.

    Worth pinning because the alternative reading - "confirmed means absorbing" -
    is the one that silently corrupts a curated label with the wrong members.
    """
    session = _session()
    session.feed("pick up the cube")
    session.confirm(0)
    event = session.feed("wipe the counter")
    assert event["outcome"] == "new cluster"
    assert event["cluster"] == 1
    assert session.model.clusters[0].count == 1


# ------------------------------------------------------------------ the state


def test_the_snapshot_reports_arrivals_clusters_and_freezes() -> None:
    session = _session()
    session.feed("pick up the cube")
    session.feed("pick up the big cube")
    session.feed("wipe the counter")
    session.confirm(0, "picking")

    state = session.snapshot()
    assert state["arrived"] == 3
    assert state["clusters"] == 2
    assert state["frozen"] == 1
    assert state["labels"] == {"0": "picking"}
    assert state["sizes"] == {0: 2, 1: 1}
    assert state["config"]["mask_verbs"] is False
    assert len(state["log"]) == 3


def test_the_snapshot_names_the_configuration_it_is_running() -> None:
    """The page prints this line, and a reader has to be able to trust it."""
    session = _session(radius=0.15, rule="running_mean", mask_verbs=True)
    config = session.snapshot()["config"]
    assert config == {
        "encoder": "word_axes",
        "radius": 0.15,
        "colour_weight": 0.0,
        "mask_verbs": True,
        "rule": "running_mean",
    }


def test_the_snapshot_of_a_fresh_session_is_empty_but_well_formed() -> None:
    state = _session().snapshot()
    assert state["arrived"] == 0
    assert state["clusters"] == 0
    assert state["sizes"] == {}
    assert state["log"] == []


def test_the_log_arrives_newest_first() -> None:
    session = _session()
    session.feed_all(["alpha one", "beta two"])
    assert [entry["text"] for entry in session.snapshot()["log"]] == ["beta two", "alpha one"]


# ------------------------------------------------------------------- figures


def test_figures_render_before_the_first_arrival() -> None:
    """An empty session is what the page shows while the model loads."""
    figures = _session().figures()
    assert set(figures) == {"treemap", "map", "heatmap"}
    for svg in figures.values():
        ET.fromstring(svg)


def test_every_figure_is_well_formed_svg_once_strings_arrive() -> None:
    session = _session(colour_weight=1.0)
    session.feed("pick up the red cube")
    session.feed("pick up the blue cube")
    session.feed("wipe the counter")
    for name, svg in session.figures().items():
        assert ET.fromstring(svg).tag.endswith("svg"), name
    assert "3 strings" in session.figures()["treemap"]


def test_the_page_only_fetches_routes_the_server_answers() -> None:
    """The page is a string; a renamed route would otherwise fail silently."""
    for route in ("/state", "/confirm/", "treemap", "map", "heatmap"):
        assert route in watch._PAGE


def test_the_seed_list_has_no_duplicates() -> None:
    """A duplicate in the seed would make the first-arrival case look like a join."""
    assert len(set(watch.SEED)) == len(watch.SEED)


@pytest.mark.parametrize("text", list(watch.SEED))
def test_every_seed_string_is_covered_by_the_verb_lexicon(text: str) -> None:
    """Only the seed, not a claim about real text: see EXP-2.5-07."""
    from experiments.clustering import attributes

    assert attributes.verb_of(text), f"seed string with no recognised verb: {text}"
