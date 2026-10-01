"""Tests for the separability diagnostic, the supervised ceiling, and the verify gate.

These exist because each one changed a decision, and each encodes a way the
measurement could quietly lie.

All of them run on the `null` encoder or on small hand-built fixtures. None needs
a model downloaded, so none can fail for a reason unrelated to the code.
"""

from __future__ import annotations

import numpy as np
import pytest
from experiments.clustering import embeddings
from experiments.clustering.evaluation import separability as sep
from experiments.clustering.evaluation.gold import GoldPair

#: Six strings arranged as three tight pairs: two positives and one negative that
#: sits far from both. Crucially each string appears in exactly one pair. An
#: earlier fixture reused a string in a positive *and* a negative pair, which
#: gives it two labels and makes any nearest-neighbour ceiling meaningless - the
#: non-independence documented in EXP-2.5-01.
_POS_A = np.array([1.0, 0.0], dtype=np.float32)
_POS_B = np.array([0.99, 0.14], dtype=np.float32)
_POS_C = np.array([0.0, 1.0], dtype=np.float32)
_POS_D = np.array([0.14, 0.99], dtype=np.float32)
_NEG_A = np.array([-1.0, 0.0], dtype=np.float32)
_NEG_B = np.array([0.0, -1.0], dtype=np.float32)
#: A *second* negative pair for the ceiling test, where the two negatives sit
#: close together. Separability and the supervised ceiling want opposite
#: geometry - a threshold separates classes when negatives are far apart, while
#: leave-one-out 1-NN scores well only when each class forms its own cluster -
#: so they get separate fixtures rather than one compromise that lies to both.
_NEG_C = np.array([-0.71, -0.70], dtype=np.float32)
_NEG_D = np.array([-0.95, -0.31], dtype=np.float32)


def _unit(value: np.ndarray) -> np.ndarray:
    return value / np.linalg.norm(value)


def _two_classes() -> dict[str, np.ndarray]:
    return {
        "a": _unit(_POS_A),
        "b": _unit(_POS_B),
        "c": _unit(_POS_C),
        "d": _unit(_POS_D),
        "e": _unit(_NEG_A),
        "f": _unit(_NEG_B),
    }


def _clustered_negatives() -> dict[str, np.ndarray]:
    """Both classes form clusters, which is the geometry 1-NN can score well on."""
    vectors = _two_classes()
    vectors["g"] = _unit(_NEG_C)
    vectors["h"] = _unit(_NEG_D)
    return vectors


def _clean_pairs() -> tuple[GoldPair, ...]:
    return (
        GoldPair("a", "b", True, True),
        GoldPair("c", "d", True, True),
        GoldPair("e", "f", False, False),
    )


@pytest.mark.unit
def test_separable_classes_are_reported_as_clean() -> None:
    report = sep.separability(_two_classes(), _clean_pairs())["action"]
    assert report.clean_threshold is not None
    assert report.best_accuracy == pytest.approx(1.0)


@pytest.mark.unit
def test_interleaved_classes_are_reported_as_not_clean() -> None:
    """The finding in EXP-2.5-01: overlap must be visible, not averaged away.

    `g` is placed next to `a`, so the negative pair (a, g) scores as high as the
    positive pair (a, b) and no threshold can separate them.
    """
    vectors = _two_classes()
    vectors["g"] = _unit(_POS_A + 0.02 * _POS_C)
    pairs = (
        GoldPair("a", "b", True, True),
        GoldPair("a", "g", False, False),
    )
    report = sep.separability(vectors, pairs)["action"]
    assert report.clean_threshold is None


@pytest.mark.unit
def test_a_view_with_no_decided_pairs_is_refused() -> None:
    with pytest.raises(ValueError, match="no decided pairs"):
        sep.separability({"a": _unit(_POS_A), "b": _unit(_POS_B)}, ())


def _probe_fixture():
    """Eight same-task pairs and eight different-task pairs, cleanly split.

    A same pair is a point and a near-copy of itself; a different pair is a point
    and its negation. Both classes are present on both sides of the split, because
    balanced accuracy is undefined with only one.
    """
    rng = np.random.default_rng(4)
    vectors: dict[str, np.ndarray] = {}
    same: list[GoldPair] = []
    different: list[GoldPair] = []
    for index in range(8):
        centre = rng.normal(size=6).astype(np.float32)
        centre = _unit(centre)
        near = _unit(centre + 0.01 * rng.normal(size=6).astype(np.float32))
        vectors[f"same_a{index}"] = centre
        vectors[f"same_b{index}"] = near
        same.append(GoldPair(f"same_a{index}", f"same_b{index}", True, True))
        vectors[f"diff_a{index}"] = centre
        vectors[f"diff_b{index}"] = _unit(-centre)
        different.append(GoldPair(f"diff_a{index}", f"diff_b{index}", False, False))
    pairs = tuple(same) + tuple(different)
    dev = (same[:4], different[:4])
    heldout = (same[4:], different[4:])
    return vectors, pairs, dev, heldout


@pytest.mark.unit
def test_the_probe_reproduces_a_learnable_pattern() -> None:
    """Sanity check on the diagnostic itself: a separable set must probe at 1.0.

    Without this, a probe of 0.82 in the experiment record could just mean the
    measurement is broken rather than the representation being good.
    """
    vectors, _pairs, (dev_same, dev_diff), (held_same, held_diff) = _probe_fixture()
    dev = dev_same + dev_diff
    heldout = held_same + held_diff
    result = sep.probe(vectors, dev, heldout, "action", range(len(dev)))
    assert result.heldout_balanced_accuracy == pytest.approx(1.0)
    assert result.headroom == pytest.approx(0.5)
    assert result.dev_pairs == len(dev)
    assert result.heldout_pairs == len(heldout)


@pytest.mark.unit
def test_the_probe_scores_pairs_not_strings() -> None:
    """The label belongs to the pair, so the counts are pairs.

    The first implementation scored a per-string nearest neighbour, which stamped
    each pair's label onto both of its strings. 27% of the constructed set's
    strings carry both labels that way, so it answered at chance and was wrong.
    """
    vectors, _pairs, (dev_same, dev_diff), (held_same, held_diff) = _probe_fixture()
    dev = dev_same + dev_diff
    heldout = held_same + held_diff
    result = sep.probe(vectors, dev, heldout, "action", range(len(dev)))
    assert result.dev_pairs == len(dev) == 8
    assert result.heldout_pairs == len(heldout) == 8


@pytest.mark.unit
def test_the_probe_beats_a_threshold_on_the_separable_fixture() -> None:
    """A probe that loses to a plain threshold would be a feature-set artefact."""
    vectors, pairs, _dev, _heldout = _probe_fixture()
    report = sep.separability(vectors, pairs)["action"]
    assert report.false_merges == 0
    assert report.false_splits == 0


@pytest.mark.unit
def test_ambiguous_pairs_are_excluded_from_the_probe() -> None:
    pairs = (GoldPair("a", "b", None, True, "arguable"),)
    with pytest.raises(ValueError, match="no decided pairs"):
        sep.probe(_two_classes(), pairs, (), "action")


@pytest.mark.unit
def test_the_null_encoder_passes_verification() -> None:
    """The control backend has to survive the gate it is measured against."""
    assert embeddings.verify(embeddings.NullEncoder()).ok


@pytest.mark.unit
def test_verification_rejects_an_encoder_with_constant_output() -> None:
    class Constant:
        name, dim = "constant", 8

        def encode(self, texts: object) -> np.ndarray:
            rows = np.ones((len(texts), 8), dtype=np.float32)  # type: ignore[arg-type]
            return rows

        def fingerprint(self) -> str:
            return "constant"

    result = embeddings.verify(Constant())  # type: ignore[arg-type]
    assert not result.ok
    assert result.failures


@pytest.mark.unit
def test_verification_rejects_a_non_deterministic_encoder() -> None:
    class Drifting:
        name, dim = "drifting", 8
        _calls = 0

        def encode(self, texts: object) -> np.ndarray:
            self._calls += 1
            rows = np.zeros((len(texts), 8), dtype=np.float32)  # type: ignore[arg-type]
            for row in range(rows.shape[0]):
                rows[row, 0] = 1.0
            rows[0, 1] = float(self._calls)  # type: ignore[arg-type]
            return rows

        def fingerprint(self) -> str:
            return "drifting"

    result = embeddings.verify(Drifting())  # type: ignore[arg-type]
    assert not result.ok
    assert any("deterministic" in failure for failure in result.failures)


@pytest.mark.unit
def test_verification_cannot_catch_an_inverted_encoder_so_none_is_faked() -> None:
    """Documents a limit of the gate rather than pretending past it.

    An encoder that scored unrelated text *above* identical text would be the
    classic mis-wired-mean-pooling failure. It cannot be constructed from text
    alone here, because the gate probes with two identical strings and any
    deterministic encoder scores those 1.0 by construction. Detecting inversion
    needs a second reference implementation, not a hand-built fake, so the gate
    covers shape, finiteness, determinism and the ordering of one identical pair
    against one unrelated pair - and this test records why that is the boundary.
    """
    result = embeddings.verify(embeddings.NullEncoder())
    assert result.ok
    assert result.failures == ()


@pytest.mark.unit
def test_is_contextual_distinguishes_a_forward_pass_from_an_average() -> None:
    """Results must not be compared across encoders without saying which is which."""
    assert not embeddings.is_contextual(embeddings.NullEncoder())
    assert not embeddings.is_semantic(embeddings.NullEncoder())
