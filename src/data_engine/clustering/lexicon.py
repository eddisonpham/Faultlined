"""The word lists the task-string extractor works from."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "the", "this", "that", "these", "those", "it", "its", "them", "then"}
)

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
        """Build from a document, keeping only the keys it actually carries."""
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


AXES: tuple[str, ...] = ("verb", "colour", "site")


@dataclass(frozen=True, slots=True)
class Ignored:
    """Which axes this extraction ignores."""

    verb: bool = True
    colour: bool = True
    site: bool = False
    extra: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, value: str | None) -> Ignored:
        """`"verb,colour,site"` in, an `Ignored` out."""
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
