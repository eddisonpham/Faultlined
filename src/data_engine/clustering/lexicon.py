"""The word lists the task-string extractor works from.

These are data, not code, and they are the weakest link in the whole clustering
surface. EXP-2.5-08 measured what happens when a list is wrong: extract too little and
the same task splits into a dozen clusters; extract too much and two unrelated tasks
collapse into one. A cluster proposal built on a bad word list is confidently wrong,
so the lists live in their own module, ship with their provenance, and can be replaced
at runtime without touching the engine.

**Where these came from.** The verb phrases and colours are the lists the stage 2.5
experiments used, copied so the product and the evidence agree. They were built by
reading 46 hand-written strings, then extended by looking at failures *inside the
evaluation set*, which is why their measured coverage on those strings is 100% and
their coverage on real operator text is unknown. The verb coverage actually measured on
a corpus that deliberately contained out-of-lexicon verbs was 0.82
(`experiments/clustering/results/README.md`, EXP-2.5-08). Nothing here should be read
as 100%.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Verb phrases, longest first at match time so "pick up" wins over "pick".
#: Multi-word entries first matters: matching "pick" alone would leave "up the mug"
#: behind and produce a core that still contains a verb.
DEFAULT_VERBS: tuple[str, ...] = (
    "pick up",
    "put down",
    "set down",
    "hand over",
    "screw in",
    "step on",
    "wipe down",
    "grab",
    "take",
    "lift",
    "raise",
    "place",
    "open",
    "close",
    "shut",
    "push",
    "pull",
    "press",
    "wipe",
    "pour",
    "fold",
    "unfold",
    "insert",
    "turn on",
    "turn off",
    "rotate",
    "unscrew",
    "give",
    "sort",
    "sweep",
    "collect",
    "gather",
    "clean",
    "carry",
    "move",
    "hold",
    "stack",
    "put",
    "pick",
    "bring",
    "drop",
    "load",
    "unload",
    "scoop",
    "stir",
    "serve",
    "screw",
    "plug",
    "unplug",
)

#: Colour words lifted out of the string when the colour axis is ignored.
DEFAULT_COLOURS: frozenset[str] = frozenset(
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
        "light",
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

#: Articles and pronouns. Always removed: they carry no object identity, and leaving
#: them in splits "the mug" from "a mug" into two clusters.
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "the", "this", "that", "these", "those", "it", "its", "them", "then"}
)

#: Prepositions that start a destination phrase. Used only when the operator asks for
#: locations to be ignored: everything from the first of these onwards is dropped.
DEFAULT_PREPOSITIONS: frozenset[str] = frozenset(
    {
        "into",
        "onto",
        "in",
        "on",
        "to",
        "from",
        "off",
        "over",
        "under",
        "behind",
        "beside",
        "next",
        "near",
        "across",
        "around",
        "at",
        "by",
        "with",
        "and",
    }
)


@dataclass(frozen=True, slots=True)
class Lexicon:
    """Word lists for extraction, with the axes an operator may choose to ignore."""

    verbs: tuple[str, ...] = DEFAULT_VERBS
    colours: frozenset[str] = DEFAULT_COLOURS
    stopwords: frozenset[str] = DEFAULT_STOPWORDS
    prepositions: frozenset[str] = DEFAULT_PREPOSITIONS

    def to_json(self) -> dict[str, Any]:
        return {
            "verbs": list(self.verbs),
            "colours": sorted(self.colours),
            "stopwords": sorted(self.stopwords),
            "prepositions": sorted(self.prepositions),
        }

    @classmethod
    def from_json(cls, document: dict[str, Any]) -> Lexicon:
        """Build from a document, keeping only the keys it actually carries.

        A partial override is the normal case: an operator who finds a missing verb
        should not have to restate the other three hundred words.
        """
        return cls(
            verbs=tuple(document.get("verbs", DEFAULT_VERBS)),
            colours=frozenset(document.get("colours", DEFAULT_COLOURS)),
            stopwords=frozenset(document.get("stopwords", DEFAULT_STOPWORDS)),
            prepositions=frozenset(document.get("prepositions", DEFAULT_PREPOSITIONS)),
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Lexicon:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))


#: The axes the extractor can be asked to ignore, in the order the UI lists them.
#: Each one is a decision with a cost, so each is opt-in rather than assumed.
AXES: tuple[str, ...] = ("verb", "colour", "site")


@dataclass(frozen=True, slots=True)
class Ignored:
    """Which axes this extraction ignores.

    The field defaults and `parse` deliberately disagree, and the reason is measured.
    `Ignored()` drops the verb and the colour, which is what groups 48 objects into 47
    clusters with a B-cubed of 0.986 - the only configuration that produced real groups.
    `parse("")` means *nothing* is ignored, because that is what an empty checkbox group
    on the Clusters page means, and silently reading it as the useful default would
    report a fragmentation that the operator did not ask for.
    """

    verb: bool = True
    colour: bool = True
    site: bool = False
    #: Axes beyond the three, for forward compatibility with a learned extractor.
    extra: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, value: str | None) -> Ignored:
        """`"verb,colour,site"` in, an `Ignored` out.

        Empty means nothing ignored, which is deliberately *not* the field default; see
        the class docstring for why the two differ.
        """
        wanted = {part.strip().lower() for part in (value or "").split(",") if part.strip()}
        return cls(
            verb="verb" in wanted,
            colour="colour" in wanted,
            site="site" in wanted,
            extra=frozenset(wanted - set(AXES)),
        )

    def names(self) -> list[str]:
        return [axis for axis in AXES if getattr(self, axis)]

    def describe(self) -> str:
        ignored = self.names()
        return ", ".join(ignored) if ignored else "nothing"
