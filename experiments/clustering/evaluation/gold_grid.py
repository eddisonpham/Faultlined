"""A constructed gold set: labels that are exact by construction, not by judgement."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from experiments.clustering.evaluation.gold import GoldPair
from experiments.clustering.evaluation.splits import Split

VERB_CLASSES: dict[str, tuple[str, ...]] = {
    "pick_up": ("pick up the {o}", "grab the {o}", "take the {o}", "lift the {o}"),
    "place": ("place the {o}", "put down the {o}", "set down the {o}"),
    "open": ("open the {o}",),
    "close": ("close the {o}",),
    "push": ("push the {o}",),
    "pull": ("pull the {o}",),
    "press": ("press the {o}",),
    "wipe": ("wipe the {o}", "wipe down the {o}"),
    "pour": ("pour the {o}",),
    "fold": ("fold the {o}",),
    "insert": ("insert the {o}",),
    "turn": ("turn the {o}", "rotate the {o}"),
    "screw_in": ("screw in the {o}",),
    "unscrew": ("unscrew the {o}",),
    "hand_over": ("hand over the {o}", "give the {o}"),
    "sort": ("sort the {o} into the bin",),
    "sweep": ("sweep the {o} into the bin",),
}

OBJECT_CLASSES: dict[str, tuple[str, ...]] = {
    "cube": ("red cube", "blue cube", "green cube"),
    "block": ("red block", "blue block", "green block"),
    "dish": ("bowl", "plate"),
    "cup": ("cup", "mug"),
    "bottle": ("bottle", "flask"),
    "drawer": ("drawer",),
    "cabinet": ("cabinet",),
    "lid": ("lid", "cap"),
    "surface": ("table", "counter"),
    "cloth": ("cloth", "towel"),
    "peg": ("peg", "rod"),
    "bolt": ("bolt", "screw"),
    "button": ("red button", "blue button"),
    "knob": ("knob",),
    "valve": ("valve",),
    "cart": ("cart", "trolley"),
    "crate": ("crate",),
}

_VERBS = tuple(VERB_CLASSES)
_OBJECTS = tuple(name for names in OBJECT_CLASSES.values() for name in names)

_NEEDS_BIN = frozenset({"sort", "sweep"})

_VERB_PAIRS = tuple(itertools.combinations(_VERBS, 2))
_OBJECT_PAIRS = tuple(itertools.combinations(_OBJECTS, 2))
_PARAPHRASE_PAIRS = tuple(
    (verb, left, right)
    for verb, templates in VERB_CLASSES.items()
    for left, right in itertools.combinations(range(len(templates)), 2)
)

CELLS = (
    "same_action_same_object",
    "same_action_diff_object",
    "diff_action_same_object",
    "diff_action_diff_object",
)


@dataclass(frozen=True, slots=True)
class GridPair:
    """A constructed pair, with the two cells it was drawn from."""

    a: str
    b: str
    same_action: bool
    same_object: bool
    cell: str
    action_class: str = ""
    object_class: str = ""

    def as_gold(self) -> GoldPair:
        return GoldPair(self.a, self.b, self.same_action, self.same_object)


def _describe(verb_class: str, object_name: str, variant: int) -> str:
    templates = VERB_CLASSES[verb_class]
    return templates[variant % len(templates)].format(o=object_name)


def _cell(same_action: bool, same_object: bool) -> str:
    if same_action and same_object:
        return "same_action_same_object"
    if same_action:
        return "same_action_diff_object"
    if same_object:
        return "diff_action_same_object"
    return "diff_action_diff_object"


def _pairs_for_cell(cell: str, limit: int) -> list[GridPair]:
    """Pairs from one cell, spread across verb classes and objects."""
    out: list[GridPair] = []
    seen: set[tuple[str, str]] = set()

    if cell == "same_action_same_object":
        candidates = [(v, t1, v, t2, o, o) for v, t1, t2 in _PARAPHRASE_PAIRS for o in _OBJECTS]
    elif cell == "same_action_diff_object":
        candidates = [(v, 0, v, 0, o1, o2) for v in _VERBS for o1, o2 in _OBJECT_PAIRS]
    elif cell == "diff_action_same_object":
        candidates = [(v1, 0, v2, 0, o, o) for v1, v2 in _VERB_PAIRS for o in _OBJECTS]
    else:
        candidates = [(v1, 0, v2, 0, o1, o2) for v1, v2 in _VERB_PAIRS for o1, o2 in _OBJECT_PAIRS]

    step = max(1, len(candidates) // max(limit, 1))
    for index in range(0, len(candidates), step):
        v1, t1, v2, t2, o1, o2 = candidates[index]
        if any(v in _NEEDS_BIN for v in (v1, v2)) and not (_bin_able(o1) and _bin_able(o2)):
            continue
        a = _describe(v1, o1, t1)
        b = _describe(v2, o2, t2)
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        same_action = v1 == v2
        same_object = o1 == o2
        if _cell(same_action, same_object) != cell:
            continue
        seen.add(key)
        out.append(
            GridPair(
                a=a,
                b=b,
                same_action=same_action,
                same_object=same_object,
                cell=cell,
                action_class=v1 if same_action else f"{v1}|{v2}",
                object_class=o1 if same_object else f"{o1}|{o2}",
            )
        )
        if len(out) >= limit:
            break
    return out


_NOT_BINNABLE = frozenset({"drawer", "cabinet", "surface", "knob", "valve", "bolt", "peg"})


def _bin_able(object_name: str) -> bool:
    return object_name not in _NOT_BINNABLE


MAX_COMPONENT_SHARE = 0.2

DEFAULT_PER_CELL = 100


def constructed_pairs(per_cell: int = DEFAULT_PER_CELL) -> tuple[GridPair, ...]:
    """A balanced constructed set, `per_cell` pairs from each of the four cells."""
    out: list[GridPair] = []
    for cell in CELLS:
        out.extend(_pairs_for_cell(cell, per_cell))
    return tuple(out)


def as_gold_pairs(pairs: tuple[GridPair, ...]) -> tuple[GoldPair, ...]:
    return tuple(pair.as_gold() for pair in pairs)


def summary(pairs: tuple[GridPair, ...]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(CELLS, 0)
    for pair in pairs:
        counts[pair.cell] += 1
    result = {
        "pairs": len(pairs),
        "distinct_strings": len({s for p in pairs for s in (p.a, p.b)}),
        "same_action": sum(1 for p in pairs if p.same_action),
        "same_object": sum(1 for p in pairs if p.same_object),
    }
    result.update(counts)
    return result


def vocab(pairs: tuple[GridPair, ...]) -> tuple[str, ...]:
    return tuple(sorted({s for p in pairs for s in (p.a, p.b)}))


def split(pairs: tuple[GridPair, ...]) -> tuple[tuple[GridPair, ...], tuple[GridPair, ...], Split]:
    """Dev/held-out over leakage components, reusing the same machinery."""
    from experiments.clustering.evaluation import splits

    groups = _string_groups(pairs)
    pair_components = [frozenset((groups[p.a], groups[p.b])) for p in pairs]

    sizes: dict[int, int] = {}
    for components in pair_components:
        for component in components:
            sizes[component] = sizes.get(component, 0) + 1

    target = len(pairs) * 0.5
    dev_ids: set[int] = set()
    running = 0
    for component, size in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
        if running >= target:
            break
        dev_ids.add(component)
        running += size

    dev = tuple(p for p, comps in zip(pairs, pair_components, strict=True) if comps & dev_ids)
    heldout = tuple(
        p for p, comps in zip(pairs, pair_components, strict=True) if not comps & dev_ids
    )

    largest = max(sizes.values()) if sizes else 0
    if pairs and largest > MAX_COMPONENT_SHARE * len(pairs):
        raise ValueError(
            f"the constructed set collapsed: one component holds {largest} of "
            f"{len(pairs)} pairs, so held-out has only {len(heldout)}. Lower "
            f"per_cell (100 is the measured maximum that still splits cleanly)."
        )
    if not dev or not heldout:
        raise ValueError(f"degenerate split: dev={len(dev)} heldout={len(heldout)}")

    return (
        dev,
        heldout,
        splits.Split(
            dev=tuple(p.as_gold() for p in dev),
            heldout=tuple(p.as_gold() for p in heldout),
            dev_components=frozenset(dev_ids),
            heldout_components=frozenset(sizes) - dev_ids,
            components=len(sizes),
            largest_component=largest,
            dev_pair_groups=tuple(
                min(comps)
                for p, comps in zip(pairs, pair_components, strict=True)
                if comps & dev_ids
            ),
            heldout_pair_groups=tuple(
                min(comps)
                for p, comps in zip(pairs, pair_components, strict=True)
                if not comps & dev_ids
            ),
        ),
    )


def _string_groups(pairs: tuple[GridPair, ...]) -> dict[str, int]:
    """Group strings that are the same task, or two phrasings of the same task."""
    from experiments.clustering.evaluation import splits

    strings = vocab(pairs)
    index = {text: i for i, text in enumerate(strings)}
    parent = list(range(len(strings)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def join(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for pair in pairs:
        join(index[pair.a], index[pair.b])

    by_class: dict[str, int] = {}
    for pair in pairs:
        for text, action, obj in ((pair.a, *_classes(pair, "a")), (pair.b, *_classes(pair, "b"))):
            key = f"{action}\x00{obj}"
            if key in by_class:
                join(index[text], by_class[key])
            else:
                by_class[key] = index[text]

    tokens = {text: splits._tokens(text) for text in strings}
    for i, left in enumerate(strings):
        for right in strings[i + 1 :]:
            if splits.jaccard_of_tokens(tokens[left], tokens[right]) >= splits.JACCARD_THRESHOLD:
                join(i, index[right])

    roots: dict[int, int] = {}
    out: dict[str, int] = {}
    for text in strings:
        root = find(index[text])
        if root not in roots:
            roots[root] = len(roots)
        out[text] = roots[root]
    return out


def string_classes(
    pairs: tuple[GridPair, ...],
) -> dict[str, tuple[str, str]]:
    """Map each distinct string to the `(verb class, object name)` it was built from."""
    out: dict[str, tuple[str, str]] = {}
    for pair in pairs:
        for side in ("a", "b"):
            out[pair.a if side == "a" else pair.b] = _classes(pair, side)
    return out


def _classes(pair: GridPair, side: str) -> tuple[str, str]:
    """The (verb class, object name) a string was generated from."""
    parts = (pair.action_class, pair.object_class)
    picked = tuple(value.split("|")[0 if side == "a" else -1] for value in parts)
    return picked[0], picked[1]
