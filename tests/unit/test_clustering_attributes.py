"""Tests for the colour facet.

EXP-2.5-05 fixed the object view's only two false merges, both of which were
pairs differing by a colour adjective. The temptation was to strip the colour word
before embedding, which would send "press the red button" and "press the blue
button" to the same string and make the merge certain. These tests exist mainly to
keep that mistake from being reintroduced.
"""

from __future__ import annotations

import numpy as np
import pytest
from experiments.clustering import attributes


class _ColourBlind:
    """An encoder that cannot see the difference between two sentences.

    Deliberately the worst case: it returns one fixed vector for everything, which
    is what a colour-blind representation looks like to the clustering. Any
    separation the facet achieves here is entirely the facet's doing.
    """

    name = "colour_blind"
    dim = 6

    def __init__(self) -> None:
        self._vector = np.full(6, 0.4)

    def encode(self, texts: object) -> np.ndarray:
        rows = list(texts)  # type: ignore[call-overload]
        return np.stack([self._vector for _ in rows])

    def fingerprint(self) -> str:
        return "colour-blind"


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    return float(left @ right / (np.linalg.norm(left) * np.linalg.norm(right)))


# ---------------------------------------------------------------- splitting


def test_colour_is_lifted_out_of_the_text_and_returned() -> None:
    bare, colours = attributes.split_colour("press the red button")
    assert bare == "press the button"
    assert colours == ["red"]


def test_multiple_colours_are_all_kept() -> None:
    bare, colours = attributes.split_colour("sort the red and blue blocks into the bin")
    assert "red" in colours and "blue" in colours
    assert "red" not in bare and "blue" not in bare


def test_a_string_with_no_colour_is_left_alone() -> None:
    bare, colours = attributes.split_colour("open the drawer")
    assert bare == "open the drawer"
    assert colours == []


def test_punctuation_does_not_hide_a_colour() -> None:
    _bare, colours = attributes.split_colour("push the red cube, then the blue one.")
    assert colours == ["red", "blue"]


def test_a_string_of_only_colour_words_is_not_emptied() -> None:
    """Stripping must never produce empty text, or the encoder gets nothing."""
    bare, _colours = attributes.split_colour("red")
    assert bare


# ---------------------------------------------------------------- the facet


def test_the_facet_separates_colours_an_encoder_cannot() -> None:
    """The whole point, stated as a test.

    Stripping colour alone would send both of these to the identical string "press
    the button", so a colour-blind encoder makes them the same point. The facet has
    to keep them apart on its own.
    """
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]

    plain = attributes.encode_with_colours(encoder, texts, 0.0)
    faceted = attributes.encode_with_colours(encoder, texts, 1.0)

    assert _cosine(plain[texts[0]], plain[texts[1]]) == pytest.approx(1.0)
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) < 0.95


def test_identical_colour_stays_identical() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "press the red button"]
    faceted = attributes.encode_with_colours(encoder, texts, 1.0)
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) == pytest.approx(1.0)


def test_zero_weight_reproduces_the_plain_encoder() -> None:
    """The feature is switchable, so every earlier result stays reproducible."""
    encoder = _ColourBlind()
    texts = ["press the red button", "open the drawer"]
    off = attributes.encode_with_colours(encoder, texts, 0.0)
    assert set(off) == set(texts)
    for text in texts:
        assert float(np.linalg.norm(off[text])) == pytest.approx(1.0, abs=1e-9)
    # No colour facet is appended at all, so the dimension is the encoder's.
    assert off[texts[0]].shape == (encoder.dim,)


def test_vectors_come_back_unit_length() -> None:
    """The clustering compares against a radius in cosine units.

    Augmented vectors have a longer norm than the encoder's output, and the method
    computes `1 - dot(point, centroid)`, which only reads as a cosine distance when
    the point is a unit vector. Unnormalised, the reported distances run negative and
    the radius stops meaning what it says.
    """
    encoder = _ColourBlind()
    for weight in (0.0, 0.5, 1.0, 4.0):
        vectors = attributes.encode_with_colours(
            encoder, ["press the red button", "open the drawer"], weight
        )
        for vector in vectors.values():
            assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-9)


def test_a_larger_weight_separates_colours_more() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]

    def gap(weight: float) -> float:
        vectors = attributes.encode_with_colours(encoder, texts, weight)
        return _cosine(vectors[texts[0]], vectors[texts[1]])

    assert gap(2.0) < gap(1.0) < gap(0.25)


def test_uncoloured_strings_get_the_empty_slot_not_a_colour() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "open the drawer"]
    faceted = attributes.encode_with_colours(encoder, texts, 2.0)
    # Distinct vectors: the uncoloured sentence is not pushed into some colour's slot.
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) < 0.999


def test_every_string_is_keyed_by_its_original_text() -> None:
    """Callers resolve gold pairs against the text they were written with."""
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]
    vectors = attributes.encode_with_colours(encoder, texts, 1.0)
    assert set(vectors) == set(texts)


def test_colour_space_has_a_slot_for_every_colour_plus_uncoloured() -> None:
    assert attributes.colour_space() == len(attributes.COLOURS) + 1
    for colour in attributes.COLOURS:
        assert 1 <= attributes.colour_index(colour) < attributes.colour_space()


def test_the_audit_can_name_a_colour() -> None:
    assert attributes.colour_of("press the red button") == "red"
    assert attributes.colour_of("open the drawer") == "-"
