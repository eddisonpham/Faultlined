"""Turn a task string into the part of it that should decide what it clusters with."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from data_engine.clustering.lexicon import Ignored, Lexicon

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class Extraction:
    """One task string, decomposed."""

    text: str
    core: str
    verb: str = ""
    colours: tuple[str, ...] = ()
    verb_matched: bool = False
    single_token: bool = False

    @property
    def usable(self) -> bool:
        """False when nothing survived, which must never become its own cluster."""
        return bool(self.core)


def tokens(text: str) -> list[str]:
    """Lower-cased word tokens."""
    return _WORD.findall(text.lower())


def match_verb(words: Sequence[str], lexicon: Lexicon) -> tuple[str, int]:
    """Longest leading verb phrase, and how many words it consumed."""
    best: tuple[str, int] = ("", 0)
    for phrase in lexicon.verbs:
        parts = phrase.split()
        length = len(parts)
        if length > len(words):
            continue
        if [word.lower() for word in words[:length]] == parts and length > best[1]:
            best = (phrase, length)
    return best


def extract(
    text: str, lexicon: Lexicon | None = None, *, ignored: Ignored | None = None
) -> Extraction:
    """Split one task string into verb, colours and the core that clusters."""
    lexicon = lexicon or Lexicon()
    ignored = ignored or Ignored()
    words = tokens(text)
    if not words:
        return Extraction(text=text, core="")

    verb, consumed = match_verb(words, lexicon)
    body = list(words[consumed:] if ignored.verb else words)

    colours: list[str] = []
    if ignored.colour:
        kept: list[str] = []
        for word in body:
            if word in lexicon.colours:
                colours.append(word)
            else:
                kept.append(word)
        body = kept

    body = [word for word in body if word not in lexicon.stopwords]

    if ignored.site:
        for index, word in enumerate(body):
            if word in lexicon.prepositions:
                body = body[:index]
                break

    core = " ".join(body)
    return Extraction(
        text=text,
        core=core,
        verb=verb,
        colours=tuple(colours),
        verb_matched=bool(verb),
        single_token=len(body) == 1,
    )


@dataclass(frozen=True, slots=True)
class Coverage:
    """How well the lexicon did on a real corpus, measured rather than asserted."""

    strings: int
    verb_matched: int
    single_token: int
    empty: int
    distinct_cores: int

    @property
    def verb_rate(self) -> float:
        return self.verb_matched / self.strings if self.strings else 0.0

    @property
    def single_token_rate(self) -> float:
        return self.single_token / self.strings if self.strings else 0.0

    @property
    def empty_rate(self) -> float:
        return self.empty / self.strings if self.strings else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "strings": self.strings,
            "verb_matched": self.verb_matched,
            "verb_rate": round(self.verb_rate, 4),
            "single_token": self.single_token,
            "single_token_rate": round(self.single_token_rate, 4),
            "empty": self.empty,
            "empty_rate": round(self.empty_rate, 4),
            "distinct_cores": self.distinct_cores,
        }


def extract_all(
    texts: Iterable[str], lexicon: Lexicon | None = None, *, ignored: Ignored | None = None
) -> list[Extraction]:
    return [extract(text, lexicon, ignored=ignored) for text in texts]


def coverage(extractions: Sequence[Extraction]) -> Coverage:
    return Coverage(
        strings=len(extractions),
        verb_matched=sum(1 for item in extractions if item.verb_matched),
        single_token=sum(1 for item in extractions if item.single_token),
        empty=sum(1 for item in extractions if not item.usable),
        distinct_cores=len({item.core for item in extractions}),
    )
