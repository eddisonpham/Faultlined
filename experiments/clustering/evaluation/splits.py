"""Splitting the gold set so that reported numbers are not leakage-inflated.

The obvious split - half the pairs to tune on, half to report - is wrong in a way
that is invisible in the results. The pairs are not independent. Two kinds of
leakage exist here, and both have to be closed:

1. **Pair leakage.** Half a pair is meaningless, so the two strings of a pair
   must always land on the same side.
2. **Near-duplicate leakage.** "grab the red cube" and "pick up the red cube" are
   the same content. If one is in dev and the other in test, the method has
   effectively already seen a test item.

The first version of this file grouped *strings* by lexical overlap and then
assigned each pair by its first string. That leaked: a pair whose two strings sat
in different lexical groups was assigned to one side while a different pair
sharing one of those groups went to the other, and the same group ended up on
both sides.

The fix is to treat the **pair** as the unit of leakage. Components are induced
by two edge types over strings:

- a *pair edge*, because a pair's two strings must never be separated;
- a *lexical edge* between near-identical strings, at a high threshold.

Union-find over both edge types gives components that provably contain every pair
whole. Splitting whole components is then guaranteed clean.

The dominant edge type turned out to be the pair edge, not the lexical one. The
gold set is deliberately built from adjacent families, so pairs like
("open the drawer", "pick up the bowl") chain "open the drawer" to the whole
"pick up" family. That is correct behaviour - over-grouping is the safe
direction - but it is why components are large and the split cannot be balanced
finer than whole components allow. The lexical threshold is a secondary guard.

Using words here is deliberate and is not a violation of the no-word-NLP rule.
That rule governs the clustering methods under test, not the harness. Embeddings
are deliberately not used for grouping, because they are one of the things under
evaluation: a harness that leaned on them could flatter them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from experiments.clustering.evaluation.gold import GOLD_PAIRS, GoldPair

#: Token overlap above which two strings are treated as near-identical and kept
#: together. High on purpose: a false merge only costs evaluation size, while a
#: false split leaks a test item into dev. Unrelated short task strings score
#: around 0.20 here, so 0.8 only catches genuine rephrasings.
JACCARD_THRESHOLD = 0.8

#: Share of strings targeted for the dev side, where the method is tuned.
DEV_FRACTION = 0.5


@dataclass(frozen=True, slots=True)
class Split:
    """A deterministic dev/held-out partition with a cleanliness verdict."""

    dev: tuple[GoldPair, ...]
    heldout: tuple[GoldPair, ...]
    #: Components represented on each side, passed in rather than recomputed from
    #: the pairs: a pair can span two components, so it has no single "component".
    dev_components: frozenset[int]
    heldout_components: frozenset[int]
    #: Number of leakage components the gold set collapsed into.
    components: int
    #: Largest component, in pairs. Large values mean the set is highly
    #: interconnected and no split can be balanced.
    largest_component: int

    @property
    def clean(self) -> bool:
        """True when no component is represented on both sides."""
        return not (self.dev_components & self.heldout_components)

    def report(self) -> dict[str, int | bool]:
        return {
            "dev_pairs": len(self.dev),
            "heldout_pairs": len(self.heldout),
            "components": self.components,
            "largest_component_pairs": self.largest_component,
            "shared_components": len(self.dev_components & self.heldout_components),
            "clean": self.clean,
        }


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.lower()))


def jaccard(left: str, right: str) -> float:
    """Token overlap of two task strings, in [0, 1].

    Public because the threshold is a methodological choice worth testing
    directly: a lexical edge that fires on shared function words would collapse
    unrelated families into one component.
    """
    a, b = _tokens(left), _tokens(right)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


class _Union:
    """Union-find with path halving, so grouping is transitive."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def join(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self._parent[max(a, b)] = min(a, b)


def jaccard_of_tokens(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap of two pre-tokenised strings, to avoid re-tokenising in the loop."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _components() -> tuple[list[str], dict[str, int], _Union]:
    """Union strings by gold-pair co-occurrence and by lexical near-duplicate."""
    strings = sorted({text for pair in GOLD_PAIRS for text in (pair.a, pair.b)})
    index = {text: i for i, text in enumerate(strings)}
    union = _Union(len(strings))

    for pair in GOLD_PAIRS:
        union.join(index[pair.a], index[pair.b])

    tokens = {text: _tokens(text) for text in strings}
    for i, left in enumerate(strings):
        for right in strings[i + 1 :]:
            if jaccard_of_tokens(tokens[left], tokens[right]) >= JACCARD_THRESHOLD:
                union.join(index[left], index[right])

    return strings, index, union


def component_map() -> dict[str, int]:
    """Map each distinct string to a stable leakage-component id."""
    strings, _index, union = _components()
    roots: dict[int, int] = {}
    groups: dict[str, int] = {}
    for text in strings:
        root = union.find(_index[text])
        if root not in roots:
            roots[root] = len(roots)
        groups[text] = roots[root]
    return groups


def _labelled() -> tuple[tuple[GoldPair, frozenset[int]], ...]:
    groups = component_map()
    return tuple((pair, frozenset({groups[pair.a], groups[pair.b]})) for pair in GOLD_PAIRS)


def split() -> Split:
    """Partition the gold set into dev and held-out with no shared component."""
    labelled = _labelled()
    sizes: dict[int, int] = {}
    for _pair, components in labelled:
        for component in components:
            sizes[component] = sizes.get(component, 0) + 1

    # Heaviest components to dev first, so tuning is not starved. Ties break on
    # the component id, which keeps the partition reproducible run to run.
    ranked = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))
    target = sum(sizes.values()) * (1.0 - DEV_FRACTION)

    dev_components: set[int] = set()
    running = 0
    for component, size in ranked:
        if running >= target:
            break
        dev_components.add(component)
        running += size

    dev = tuple(pair for pair, comps in labelled if comps & dev_components)
    heldout = tuple(pair for pair, comps in labelled if not comps & dev_components)
    return Split(
        dev=dev,
        heldout=heldout,
        dev_components=frozenset(dev_components),
        heldout_components=frozenset(sizes) - dev_components,
        components=len(sizes),
        largest_component=max(sizes.values()) if sizes else 0,
    )


def iter_pairs(include_ambiguous: bool = True) -> Iterator[GoldPair]:
    """Every gold pair, in file order, for clustering inputs."""
    for pair in GOLD_PAIRS:
        if include_ambiguous or not pair.ambiguous:
            yield pair
