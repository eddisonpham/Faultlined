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

    plain = attributes.encode_for_clustering(encoder, texts, 0.0)
    faceted = attributes.encode_for_clustering(encoder, texts, 1.0)

    assert _cosine(plain[texts[0]], plain[texts[1]]) == pytest.approx(1.0)
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) < 0.95


def test_identical_colour_stays_identical() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "press the red button"]
    faceted = attributes.encode_for_clustering(encoder, texts, 1.0)
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) == pytest.approx(1.0)


def test_zero_weight_reproduces_the_plain_encoder() -> None:
    """The feature is switchable, so every earlier result stays reproducible."""
    encoder = _ColourBlind()
    texts = ["press the red button", "open the drawer"]
    off = attributes.encode_for_clustering(encoder, texts, 0.0)
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
        vectors = attributes.encode_for_clustering(
            encoder, ["press the red button", "open the drawer"], weight
        )
        for vector in vectors.values():
            assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-9)


def test_a_larger_weight_separates_colours_more() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]

    def gap(weight: float) -> float:
        vectors = attributes.encode_for_clustering(encoder, texts, weight)
        return _cosine(vectors[texts[0]], vectors[texts[1]])

    assert gap(2.0) < gap(1.0) < gap(0.25)


def test_uncoloured_strings_get_the_empty_slot_not_a_colour() -> None:
    encoder = _ColourBlind()
    texts = ["press the red button", "open the drawer"]
    faceted = attributes.encode_for_clustering(encoder, texts, 2.0)
    # Distinct vectors: the uncoloured sentence is not pushed into some colour's slot.
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) < 0.999


def test_every_string_is_keyed_by_its_original_text() -> None:
    """Callers resolve gold pairs against the text they were written with."""
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]
    vectors = attributes.encode_for_clustering(encoder, texts, 1.0)
    assert set(vectors) == set(texts)


def test_colour_space_has_a_slot_for_every_colour_plus_uncoloured() -> None:
    assert attributes.colour_space() == len(attributes.COLOURS) + 1
    for colour in attributes.COLOURS:
        assert 1 <= attributes.colour_index(colour) < attributes.colour_space()


def test_the_audit_can_name_a_colour() -> None:
    assert attributes.colour_of("press the red button") == "red"
    assert attributes.colour_of("open the drawer") == "-"


# ------------------------------------------------------------ verb masking


def test_a_single_word_verb_is_masked() -> None:
    masked, matched = attributes.mask_verbs("open the drawer")
    assert matched
    assert masked == "action the drawer"


def test_a_multi_word_verb_is_masked_as_one_span() -> None:
    """Longest match first, so "pick up" is not masked as bare "pick"."""
    masked, matched = attributes.mask_verbs("pick up the red cube")
    assert matched
    assert masked == "action the red cube"
    assert attributes.mask_verbs("set down the lid")[0] == "action the lid"
    assert attributes.mask_verbs("screw in the bolt")[0] == "action the bolt"


def test_an_unknown_verb_is_reported_rather_than_guessed() -> None:
    """A missed verb is a silent no-op, so the miss has to be visible."""
    masked, matched = attributes.mask_verbs("frobnicate the widget")
    assert not matched
    assert masked == "frobnicate the widget"


def test_masking_keeps_the_rest_of_the_sentence_intact() -> None:
    """Masking, not deletion: the object and any trailing phrase must survive."""
    masked, _ = attributes.mask_verbs("place the bowl on the plate")
    assert masked == "action the bowl on the plate"
    assert "plate" in masked and "bowl" in masked


def test_two_phrasings_of_one_object_mask_to_the_same_text() -> None:
    """This is the whole mechanism.

    `place the bowl` and `lift the bowl` are the failure EXP-2.5-04 left behind. If
    they do not collapse to one input, the object view can still split them for
    disagreeing about the verb, and the feature does nothing.
    """
    assert attributes.mask_verbs("place the bowl")[0] == attributes.mask_verbs("lift the bowl")[0]
    assert attributes.mask_verbs("hand over the pen")[0] == attributes.mask_verbs("give the pen")[0]


def test_different_objects_still_mask_to_different_texts() -> None:
    """Otherwise masking would merge everything with the same verb."""
    assert (
        attributes.mask_verbs("press the red button")[0]
        != attributes.mask_verbs("press the blue button")[0]
    )
    assert (
        attributes.mask_verbs("pick up the cube")[0] != attributes.mask_verbs("pick up the mug")[0]
    )


def test_coverage_is_reported_as_a_fraction() -> None:
    texts = ["open the drawer", "pick up the cube", "frobnicate the widget"]
    assert attributes.verb_mask_coverage(texts) == pytest.approx(2 / 3)
    assert attributes.verb_mask_coverage([]) == 0.0


def test_verb_of_names_the_phrase_it_matched() -> None:
    assert attributes.verb_of("pick up the cube") == "pick up"
    assert attributes.verb_of("open the drawer") == "open"
    assert attributes.verb_of("frobnicate the widget") == "-"


def test_masking_and_colour_compose() -> None:
    """Both facets apply to one string, in either order, with the same result."""
    encoder = _ColourBlind()
    texts = ["press the red button", "press the blue button"]
    faceted = attributes.encode_for_clustering(encoder, texts, 2.0, mask_verb=True)
    assert _cosine(faceted[texts[0]], faceted[texts[1]]) < 0.95
    # Same verb, different object: still distinct.
    other = ["press the green button"]
    mixed = attributes.encode_for_clustering(encoder, [*texts, *other], 2.0, mask_verb=True)
    assert _cosine(mixed[other[0]], mixed[texts[0]]) < 0.95


def test_an_unmasked_verb_still_gets_its_colour_handled() -> None:
    """Masking failing must not disable the colour facet for that string."""
    encoder = _ColourBlind()
    texts = ["frobnicate the red widget", "press the red widget"]
    faceted = attributes.encode_for_clustering(encoder, texts, 2.0, mask_verb=True)
    # They differ by verb alone, which masking leaves in, so they may or may not
    # separate - but both must still be unit vectors of the augmented width.
    for vector in faceted.values():
        assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-9)
        assert vector.shape[0] == encoder.dim + attributes.colour_space()


def test_the_known_verb_gaps_that_measurement_found_are_closed() -> None:
    """`put` and `raise` were added after they caused a false split on the hand set."""
    for text in ("put the bowl on the plate", "raise the lid"):
        _masked, matched = attributes.mask_verbs(text)
        assert matched, text
