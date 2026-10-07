"""Handle colour adjectives so they stop deciding nothing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from experiments.clustering import embeddings


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


VERBS: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        {
            ("pick", "up"),
            ("put", "down"),
            ("set", "down"),
            ("hand", "over"),
            ("screw", "in"),
            ("step", "on"),
            ("wipe", "down"),
            ("grab",),
            ("take",),
            ("lift",),
            ("place",),
            ("open",),
            ("close",),
            ("shut",),
            ("push",),
            ("pull",),
            ("press",),
            ("wipe",),
            ("pour",),
            ("fold",),
            ("unfold",),
            ("insert",),
            ("turn",),
            ("rotate",),
            ("unscrew",),
            ("give",),
            ("sort",),
            ("sweep",),
            ("collect",),
            ("gather",),
            ("clean",),
            ("carry",),
            ("move",),
            ("hold",),
            ("stack",),
            ("put",),
            ("raise",),
        },
        key=len,
        reverse=True,
    )
)

_VERB_TOKEN = "action"


def mask_verbs(text: str) -> tuple[str, bool]:
    """Replace the leading verb phrase with a neutral token."""
    words = text.split()
    if not words:
        return text, False
    for phrase in VERBS:
        length = len(phrase)
        if len(words) >= length and tuple(w.lower().strip(".,") for w in words[:length]) == phrase:
            return " ".join([_VERB_TOKEN, *words[length:]]), True
    return text, False


def verb_mask_coverage(texts: Sequence[str]) -> float:
    """Share of strings whose leading verb was recognised."""
    if not texts:
        return 0.0
    return sum(1 for text in texts if mask_verbs(text)[1]) / len(texts)


COLOURS: frozenset[str] = frozenset(
    {
        "amber",
        "black",
        "blue",
        "brown",
        "cream",
        "cyan",
        "gold",
        "golden",
        "green",
        "grey",
        "gray",
        "magenta",
        "maroon",
        "navy",
        "olive",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "teal",
        "violet",
        "white",
        "yellow",
    }
)

_NONE = 0


def split_colour(text: str) -> tuple[str, list[str]]:
    """Return the text without its colour words, and the colours it named."""
    words = text.split()
    colours = [
        word.strip(".,!?;:").lower() for word in words if word.strip(".,!?;:").lower() in COLOURS
    ]
    kept = [word for word in words if word.strip(".,!?;:").lower() not in COLOURS]
    bare = " ".join(kept).strip()
    return (bare or text), colours


def _indicator(colours: Sequence[str], size: int) -> np.ndarray:
    """A unit-length colour vector, or the all-zero "no colour" vector."""
    vector = np.zeros(size, dtype=np.float64)
    for colour in colours:
        vector[colour_index(colour)] = 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def colour_index(colour: str) -> int:
    """Stable slot for a colour word, never colliding with the uncoloured slot."""
    return _NONE + 1 + sorted(COLOURS).index(colour)


def colour_space(colours: Iterable[str] = COLOURS) -> int:
    """Width of the colour facet: every colour plus the uncoloured slot."""
    return 1 + len(set(colours))


def encode_for_clustering(
    encoder: embeddings.Encoder,
    texts: Sequence[str],
    weight: float,
    *,
    mask_verb: bool = False,
) -> dict[str, np.ndarray]:
    """Encode `texts`, optionally lifting colour out and neutralising the verb."""
    base = embeddings.encode_all(encoder, list(texts))
    if weight <= 0.0 and not mask_verb:
        return {
            text: _unit(np.asarray(row, dtype=np.float64))
            for text, row in zip(texts, base, strict=True)
        }

    size = colour_space()
    prepared: list[str] = []
    for text in texts:
        prepared_text = mask_verbs(text)[0] if mask_verb else text
        prepared.append(split_colour(prepared_text)[0])
    stripped_vectors = embeddings.encode_all(encoder, prepared)
    out: dict[str, np.ndarray] = {}
    for text, stripped_row, colours in zip(
        texts,
        stripped_vectors,
        (split_colour(mask_verbs(text)[0] if mask_verb else text)[1] for text in texts),
        strict=True,
    ):
        row = np.asarray(stripped_row, dtype=np.float64)
        augmented = np.concatenate([_unit(row), weight * _indicator(colours, size)])
        out[text] = _unit(augmented)
    return out


def colour_of(text: str) -> str:
    """A short human-readable description of a string's colour, for the audit."""
    colours = split_colour(text)[1]
    return "+".join(colours) if colours else "-"


def verb_of(text: str) -> str:
    """The verb phrase this string starts with, or `-` when none was recognised."""
    words = [w.lower().strip(".,") for w in text.split()]
    for phrase in VERBS:
        if len(words) >= len(phrase) and tuple(words[: len(phrase)]) == phrase:
            return " ".join(phrase)
    return "-"


def masked_verb_classes(texts: Sequence[str]) -> dict[str, str]:
    """Map each string to its verb phrase, collapsing synonyms onto one class."""
    return {text: verb_of(text) for text in texts}
