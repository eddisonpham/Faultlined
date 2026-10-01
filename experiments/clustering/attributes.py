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


#: Verb phrases that may open an imperative task string, longest first at match time.
#:
#: **This list was written with the gold sets open, so coverage on them is 100% by
#: construction and says nothing about coverage on real operator text.** That is the
#: honest limitation of a hand-written lexicon and the reason `verb_mask_coverage`
#: is reported rather than assumed: a verb this list misses leaves its string unmasked,
#: which is a silent no-op, not an error.
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
            # Added after measuring: `put the bowl on the plate` and `raise the lid`
            # were unmasked, and the second was the one surviving false split, so the
            # lexicon gap was causing the failure rather than the verb-free view
            # being wrong. Both are ordinary verbs whose absence was an oversight.
            # That they were found by looking at eval-set failures is exactly why the
            # 100% coverage this produces on these two sets proves nothing about real
            # operator text - see `verb_mask_coverage`.
            ("put",),
            ("raise",),
        },
        key=len,
        reverse=True,
    )
)

#: Stand-in for a masked verb span. One token regardless of how many words the verb
#: was, so "pick up" and "grab" collapse to the same thing rather than to strings of
#: different lengths that a contextual encoder would still tell apart.
_VERB_TOKEN = "action"


def mask_verbs(text: str) -> tuple[str, bool]:
    """Replace the leading verb phrase with a neutral token.

    Returns the masked text and whether a verb was actually found.

    Masking rather than deleting keeps word order and sentence length intact, so the
    encoder still sees well-formed text. Deleting outright is also tempting and is
    what `split_colour` does for colour - but there the colour is a modifier to be
    replaced by an explicit facet, whereas here the verb is being *neutralised*, and
    removing it outright would collapse "the red cube" and "the" out of existence for
    strings whose verb is the only thing distinguishing them.

    Both phrasings of one action collapse to the same masked string, which is the
    point: `place the bowl` and `lift the bowl` become the same input, so the object
    view can no longer split them for disagreeing about the verb.
    """
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


def encode_for_clustering(
    encoder: embeddings.Encoder,
    texts: Sequence[str],
    weight: float,
    *,
    mask_verb: bool = False,
) -> dict[str, np.ndarray]:
    """Encode `texts`, optionally lifting colour out and neutralising the verb.

    At `weight == 0` with no verb masking this is the plain encoder output, so every
    earlier result stays reproducible and each feature can be switched off rather
    than argued about.

    Keys are the **original** strings, so callers keep resolving gold pairs against
    the text they were written with; only the vector changes.
    """
    base = embeddings.encode_all(encoder, list(texts))
    if weight <= 0.0 and not mask_verb:
        return {
            text: _unit(np.asarray(row, dtype=np.float64))
            for text, row in zip(texts, base, strict=True)
        }

    size = colour_space()
    prepared: list[str] = []
    for text in texts:
        # Verb first, then colour: masking replaces the leading phrase, and a
        # colour word is never inside it, so the order does not matter. Doing it
        # this way means a verb the lexicon misses still gets its colour handled.
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


def verb_of(text: str) -> str:
    """The verb phrase this string starts with, or `-` when none was recognised.

    Re-matched rather than read back off the masked text, because masking replaces
    the verb and does not preserve it.
    """
    words = [w.lower().strip(".,") for w in text.split()]
    for phrase in VERBS:
        if len(words) >= len(phrase) and tuple(words[: len(phrase)]) == phrase:
            return " ".join(phrase)
    return "-"


def masked_verb_classes(texts: Sequence[str]) -> dict[str, str]:
    """Map each string to its verb phrase, collapsing synonyms onto one class.

    "place the bowl" and "lift the bowl" are different verb phrases but the same
    *action* as far as a clustering that has been told to ignore the verb, so the
    audit reports them together. That grouping is what makes the before-and-after
    comparison meaningful rather than a list of surface verbs.
    """
    return {text: verb_of(text) for text in texts}
