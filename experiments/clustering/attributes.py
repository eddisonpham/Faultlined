"""Handle colour adjectives so they stop deciding nothing.

EXP-2.5-04 measured the object view's only two false merges, and both were the
same failure: `press the red button` merged with `press the blue button`, and
`sort the red blocks into the bin` with `sort the blue blocks into the bin`. Each
pair differs by one colour word and nothing else, and a contextual encoder embeds
the two sentences as near-identical. No radius separates them, because in that
encoder's geometry they are not two places.

**The obvious fix is the wrong one.** Stripping colour from the text before
embedding sends "press the red button" and "press the blue button" to the *same*
string, which makes the merge certain rather than merely likely. Deleting the
information and then complaining it went missing is not a fix. Colour has to stay
and become explicit.

So it is lifted out of the embedding and appended as its own orthogonal component:
the encoder sees the sentence with the colour removed - because colour's
contribution there is arbitrary noise, being whichever of two near-synonymous
vectors a tokeniser happened to produce - and a colour indicator is concatenated
afterwards. Two sentences differing only in colour then differ in a coordinate
nothing else touches, by a distance this file controls.

**Why a weight at all, and why it is swept.** Concatenating into a shared norm
trades two errors against each other. For unit base vectors with colour indicator
`w * e`:

    same colour    -> cos = (base + w^2) / (1 + w^2)
    different colour -> cos = base / (1 + w^2)

Small `w` leaves the colour signal buried in the base similarity. Large `w`
guarantees the colour gap but compresses the variation *within* a colour group
until every same-colour pair looks identical. Neither extreme is right, so the
weight is a measured hyperparameter and `just cluster colours` chooses it on dev
rather than anyone guessing.

**What this does not fix.** The lexicon is a hand-written list of colour words, so
it will miss "amber", "cream", "navy" and anything a particular site uses for its
own parts. It also carries no ordering: "red" and "blue" are orthogonal here, which
is the safe default, but a domain that genuinely treats "dark blue" as a variant
of "blue" needs that expressed explicitly. Both limitations are stated rather than
assumed away, and a learned attribute extractor is the route past them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from experiments.clustering import embeddings


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


#: Colour words treated as object facets rather than as part of the object noun.
#: Deliberately a plain set: it is the smallest thing that addresses a measured
#: failure, and every entry is one somebody had to write down.
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

#: Index 0 is "this string names no colour", so an uncoloured sentence is
#: represented rather than dropped.
_NONE = 0


def split_colour(text: str) -> tuple[str, list[str]]:
    """Return the text without its colour words, and the colours it named.

    Multiple colours are kept - "red and blue blocks" is not a grey block - and
    returned in the order written so the result is stable.
    """
    words = text.split()
    colours = [
        word.strip(".,!?;:").lower() for word in words if word.strip(".,!?;:").lower() in COLOURS
    ]
    kept = [word for word in words if word.strip(".,!?;:").lower() not in COLOURS]
    bare = " ".join(kept).strip()
    return (bare or text), colours


def _indicator(colours: Sequence[str], size: int) -> np.ndarray:
    """A unit-length colour vector, or the all-zero "no colour" vector.

    Several colours give the sum of their one-hots, normalised. Two sentences
    sharing one of two colours therefore share half a unit rather than a whole one,
    which is weaker than an exact match but far better than treating a multi-coloured
    part as uncoloured.
    """
    vector = np.zeros(size, dtype=np.float64)
    for colour in colours:
        vector[colour_index(colour)] = 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def colour_index(colour: str) -> int:
    """Stable slot for a colour word, never colliding with the uncoloured slot.

    The `+ 1` is not decoration: sorting puts "amber" first, so an index taken
    straight from the list gave amber slot 0 - the same slot as "no colour" - and
    every sentence mentioning an amber part would have been recorded as having no
    colour at all.
    """
    return _NONE + 1 + sorted(COLOURS).index(colour)


def colour_space(colours: Iterable[str] = COLOURS) -> int:
    """Width of the colour facet: every colour plus the uncoloured slot."""
    return 1 + len(set(colours))


def encode_with_colours(
    encoder: embeddings.Encoder,
    texts: Sequence[str],
    weight: float,
) -> dict[str, np.ndarray]:
    """Encode `texts`, lifting colour out of the embedding and onto its own axis.

    At `weight == 0` this is the plain encoder output, so every existing result
    stays reproducible and the feature can be switched off rather than argued about.

    Keys are the **original** strings, so callers keep resolving gold pairs against
    the text they were written with; only the vector changes.
    """
    base = embeddings.encode_all(encoder, list(texts))
    if weight <= 0.0:
        return {
            text: _unit(np.asarray(row, dtype=np.float64))
            for text, row in zip(texts, base, strict=True)
        }

    size = colour_space()
    stripped = [split_colour(text)[0] for text in texts]
    stripped_vectors = embeddings.encode_all(encoder, stripped)
    out: dict[str, np.ndarray] = {}
    for text, stripped_row, colours in zip(
        texts,
        stripped_vectors,
        (split_colour(text)[1] for text in texts),
        strict=True,
    ):
        row = np.asarray(stripped_row, dtype=np.float64)
        augmented = np.concatenate([_unit(row), weight * _indicator(colours, size)])
        # Normalised back to unit length. Cosine is scale-invariant, so this leaves
        # every pairwise similarity exactly as it was, but it keeps the downstream
        # `1 - dot(point, centroid)` a cosine distance rather than a distance times
        # sqrt(1 + w^2). Without it the reported distances run negative and the
        # radius stops meaning what it says.
        out[text] = _unit(augmented)
    return out


def colour_of(text: str) -> str:
    """A short human-readable description of a string's colour, for the audit."""
    colours = split_colour(text)[1]
    return "+".join(colours) if colours else "-"
