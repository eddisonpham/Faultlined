"""A corpus big enough to measure scale, and real strings to measure it against.

Every experiment before this one ran on 46 hand-written strings or 494 constructed
ones. That is enough to decide *whether* the object view works; it is not enough to
say what happens at a thousand, where three things change at once: the centroid of a
cluster is estimated from many more members, a single radius has to hold across far
more vocabulary, and arrival order has far more opportunities to matter.

**What this corpus can and cannot prove.** The synthetic half is generated from a
grammar I wrote, and the clustering configuration was tuned on strings I wrote, so
its purity numbers are close to circular and are reported as *scale* behaviour, not as
quality. They answer: does the method stay stable, cheap and legible as the input
grows, or does it quietly collapse into one cluster or shatter into singletons.

The real half cannot be scored at all - there are no labels for operator text - so it
is measured the only honest way: how many clusters a few hundred genuinely different
task strings produce, how many of them are singletons, and whether they land together
the way a reader would expect. Those are the numbers that would expose the synthetic
half flattering itself.
"""

from __future__ import annotations

import csv
import itertools
import json
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

#: Objects, grouped by where a robot would meet them. One noun each, because the
#: gold label is the noun: two sites for the same noun would make the label ambiguous
#: and quietly inflate every purity number in the report.
OBJECTS: tuple[tuple[str, ...], ...] = (
    (
        "mug",
        "bowl",
        "plate",
        "kettle",
        "frying pan",
        "water bottle",
        "sponge",
        "water glass",
        "cutting board",
        "salt shaker",
        "coffee mug",
        "colander",
    ),
    (
        "book",
        "pen",
        "notebook",
        "laptop",
        "computer mouse",
        "folder",
        "phone",
        "remote control",
        "sticky note",
        "scissors",
        "tape roll",
        "desk lamp",
    ),
    (
        "wrench",
        "screwdriver",
        "power drill",
        "cardboard box",
        "bolt",
        "clamp",
        "sandpaper",
        "toolbox",
        "socket",
        "measuring tape",
        "paint brush",
        "cable",
    ),
    (
        "towel",
        "bar of soap",
        "toothbrush",
        "shampoo bottle",
        "comb",
        "washcloth",
        "toilet brush",
        "sponge bar",
        "bath mat",
        "shower cap",
        "nail file",
        "cotton ball",
    ),
)

#: Verbs the shipped `attributes.VERBS` lexicon already knows, so masking applies.
KNOWN_VERBS: tuple[str, ...] = (
    "pick up",
    "put down",
    "place",
    "move",
    "push",
    "pull",
    "lift",
    "open",
    "close",
    "turn on",
    "turn off",
    "wipe",
    "pour",
    "hand",
    "stack",
    "sort",
)

#: Verbs the lexicon does **not** know. Deliberate: 100% verb coverage on the
#: hand-written strings proved nothing (EXP-2.5-07), and a corpus with no
#: out-of-lexicon verbs would keep proving nothing.
UNKNOWN_VERBS: tuple[str, ...] = (
    "tidy",
    "shove",
    "retrieve",
    "collect",
    "fetch",
    "declutter",
)

#: Colours from `attributes.COLOURS`, so the colour facet is exercised, not bypassed.
COLOURS: tuple[str, ...] = ("red", "blue", "green")

#: Neither colours nor verbs: adjectives that change the embedding without changing
#: what the object is. These are the fragmentation engine.
MODIFIERS: tuple[str, ...] = ("small", "large", "plastic", "wooden", "metal", "worn")

#: Where the object goes. Kept out of the gold label on purpose: the shipped view is
#: object-pure, so a site appearing in the text is a distractor the method must ignore.
SITES: tuple[str, ...] = (
    "on the table",
    "on the plate",
    "into the bin",
    "onto the plate",
    "in the drawer",
    "off the shelf",
    "into the sink",
    "on the counter",
    "into the box",
    "on the workbench",
    "into the cupboard",
)

DEFAULT_TARGET = 1200


@dataclass(frozen=True, slots=True)
class Item:
    """One task string and everything a report needs to say about it."""

    text: str
    #: The gold object class. Two strings with the same label should share a cluster.
    label: str
    colour: str = ""
    verb: str = ""
    modifier: str = ""
    site: str = ""
    #: `"synthetic"` or the dataset a real string came from.
    origin: str = "synthetic"
    #: True when the leading verb is outside `attributes.VERBS`, so masking cannot
    #: remove it. The honest denominator for a coverage number.
    unknown_verb: bool = False


def _forms(
    objects: Sequence[str],
    verbs: Sequence[str],
    *,
    modifiers: Sequence[str] = MODIFIERS,
    sites: Sequence[str] = SITES,
    unknown: bool = False,
) -> Iterator[Item]:
    """Every combination of one object, one verb, and optional colour/modifier/site.

    Enumerated rather than sampled: a random corpus has to be checked for balance
    after the fact, and an enumerated one is balanced by construction.
    """
    for obj, verb, colour, modifier, site in itertools.product(
        objects, verbs, ("", *COLOURS), ("", *modifiers), ("", *sites)
    ):
        parts = [f"{verb} the"]
        if colour:
            parts.append(colour)
        if modifier:
            parts.append(modifier)
        parts.append(obj)
        text = " ".join(parts)
        if site:
            text = f"{text} {site}"
        yield Item(
            text=text,
            label=obj,
            colour=colour,
            verb=verb,
            modifier=modifier,
            site=site,
            unknown_verb=unknown,
        )


def minimal_corpus(target: int = 480) -> tuple[Item, ...]:
    """The control corpus: verb and colour variation only.

    Same grammar, but every string is `verb the [colour] object`. If the clustering is
    healthy here and shatters on the full corpus, the difference is the adjectives and
    locations, not the method - which is the one thing the scale run cannot be allowed
    to leave ambiguous.
    """
    objects = [name for group in OBJECTS for name in group]
    known = list(_forms(objects, KNOWN_VERBS, modifiers=("",), sites=("",)))
    unknown = list(_forms(objects, UNKNOWN_VERBS, modifiers=("",), sites=("",), unknown=True))
    pool = known + unknown
    if target > len(pool):
        raise ValueError(f"asked for {target} strings, the grammar makes {len(pool)}")
    step = len(pool) / target
    return tuple(pool[int(index * step)] for index in range(target))


#: One fixed verb and object slot per axis run, so the only thing that varies is the
#: axis under test.
_AXIS_BASELINE: dict[str, str] = {"verb": "pick up", "colour": "", "modifier": "", "site": ""}


def axis_corpus(axis: str, values: Sequence[str]) -> tuple[Item, ...]:
    """Strings that are identical except along one axis.

    This is the experiment that decides *which* kind of variation a representation can
    survive. Hold the object, the verb, the adjective and the location fixed, vary one
    of them across every object, and ask whether same-object pairs end up closer than
    different-object pairs. If they do not, no radius can recover object identity along
    that axis, and every cluster count downstream is measuring the axis rather than the
    objects.
    """
    if axis not in ("colour", "modifier", "site", "verb"):
        raise ValueError(f"unknown axis {axis!r}")
    items: list[Item] = []
    for group in OBJECTS:
        for obj in group:
            for value in values:
                kwargs = dict(_AXIS_BASELINE)
                kwargs[axis] = value
                parts = [f"{kwargs['verb']} the"]
                if kwargs["colour"]:
                    parts.append(kwargs["colour"])
                if kwargs["modifier"]:
                    parts.append(kwargs["modifier"])
                parts.append(obj)
                sentence = " ".join(parts)
                if kwargs["site"]:
                    sentence = f"{sentence} {kwargs['site']}"
                items.append(
                    Item(
                        text=sentence,
                        label=obj,
                        colour=kwargs["colour"],
                        verb=kwargs["verb"],
                        modifier=kwargs["modifier"],
                        site=kwargs["site"],
                        unknown_verb=kwargs["verb"] not in KNOWN_VERBS,
                    )
                )
    return tuple(items)


def available() -> int:
    """How many distinct strings the grammar can produce.

    Counted arithmetically, not by building them: the pool is a quarter of a million
    rows and nothing here needs it in memory to answer the question.
    """
    objects = sum(len(group) for group in OBJECTS)
    per_form = (1 + len(COLOURS)) * (1 + len(MODIFIERS)) * (1 + len(SITES))
    return objects * (len(KNOWN_VERBS) + len(UNKNOWN_VERBS)) * per_form


def synthetic_corpus(target: int = DEFAULT_TARGET) -> tuple[Item, ...]:
    """`target` distinct strings, balanced over objects, colour and verb class.

    Balanced because an unbalanced corpus lets a method look good by predicting the
    majority class. About a quarter of the rows come from verbs the masking lexicon
    does not know, which is the honest version of the coverage claim.
    """
    objects = [name for group in OBJECTS for name in group]
    known = list(_forms(objects, KNOWN_VERBS))
    unknown = list(_forms(objects, UNKNOWN_VERBS, unknown=True))
    pool = known + unknown
    if target > len(pool):
        raise ValueError(f"asked for {target} strings, the grammar makes {len(pool)}")

    # Stride rather than a prefix: a prefix would take every form for the first few
    # objects and no forms for the rest, which is a corpus with 48 labels wearing a
    # 1200-string hat.
    step = len(pool) / target
    picked = [pool[int(index * step)] for index in range(target)]
    if len({item.text for item in picked}) != target:
        raise AssertionError("the stride produced a duplicate; the grammar is not uniform")
    return tuple(picked)


def balance(items: Sequence[Item]) -> dict[str, dict[str, int]]:
    """Counts per label, colour and verb class. Reported, so imbalance is visible."""
    out: dict[str, dict[str, int]] = {"label": {}, "colour": {}, "verb": {}, "modifier": {}}
    for item in items:
        for key, value in (
            ("label", item.label),
            ("colour", item.colour or "none"),
            ("verb", item.verb or "none"),
            ("modifier", item.modifier or "none"),
        ):
            out[key][value] = out[key].get(value, 0) + 1
    return out


# ------------------------------------------------------------------ the core


def core_text(text: str) -> str:
    """Strip a task string down to the words a gold object label is made of.

    Verb phrase replaced, colour lifted out, adjectives and location tails dropped.
    This is the pipeline the object view *wants* and does not have: it asks the
    embedding to ignore variation the method has not identified, and a sentence
    encoder will faithfully encode variation it has been asked to encode.

    Measured on the synthetic corpus this is close to circular - the strippers use the
    same lexicons the corpus was generated from - so the number here is the **headroom**,
    not a quality claim: it says how much of the scale failure is caused by variation
    the method never asked to ignore. The cost of the headroom is lexicon coverage on
    real text, which the real-string run does not have and cannot fake.
    """
    from experiments.clustering import attributes

    masked, _matched = attributes.mask_verbs(text)
    bare, _colours = attributes.split_colour(masked)
    tokens = [word.strip(".,") for word in bare.split()]
    # Start at the first determiner: everything before it is a verb phrase, and the
    # masking lexicon does not always consume all of it ("turn on the mug" masks to
    # "action on the mug"). The determiner is the one boundary every template in this
    # grammar shares, and it is where the object begins.
    start = 0
    for index, token in enumerate(tokens):
        if token in {"the", "a", "an"}:
            start = index + 1
            break
    words = [word for word in tokens[start:] if word not in MODIFIERS and word != "action"]
    # Drop a location tail: everything from the first word of a site phrase onwards.
    keys = {_without_articles(site): site for site in SITES}
    for index in range(len(words)):
        tail = " ".join(word for word in words[index:] if word not in {"the", "a", "an"})
        if tail in keys:
            words = words[:index]
            break
    return " ".join(words) or bare


def _without_articles(phrase: str) -> str:
    """`on the table` -> `on table`, matching the article-stripped token stream."""
    return " ".join(word for word in phrase.split() if word not in {"the", "a", "an"})


def core_corpus(items: Sequence[Item]) -> tuple[Item, ...]:
    """`items` with each string replaced by its core."""
    return tuple(
        replace(item, text=core_text(item.text), origin=f"{item.origin}+core") for item in items
    )


# ------------------------------------------------------------------- real text


def local_task_strings(root: Path = Path("var/real-data")) -> tuple[Item, ...]:
    """Task strings from LeRobot datasets already on this machine.

    LeRobot keeps the sentence in two places depending on version: v3 writes
    `meta/tasks.parquet` with the text in the index column, v2 writes
    `meta/tasks.csv` with a `task` column. Both are read, because a corpus that
    silently misses half the datasets on disk is worse than none.
    """
    items: list[Item] = []
    for path in sorted(root.glob("*/meta/tasks.csv")):
        dataset = path.parts[-3]
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                text = (row.get("task") or "").strip()
                if text:
                    items.append(Item(text=text, label=dataset, origin=dataset))
    for path in sorted(root.glob("*/meta/info.json")):
        dataset = path.parts[-3]
        doc = json.loads(path.read_text(encoding="utf-8"))
        for text in doc.get("tasks") or []:
            if isinstance(text, str) and text.strip():
                items.append(Item(text=text.strip(), label=dataset, origin=dataset))
    for path in sorted(root.glob("*/meta/tasks.parquet")):
        dataset = path.parts[-3]
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        column = "__index_level_0__" if "__index_level_0__" in table.column_names else "task_index"
        for value in table.column(column).to_pylist():
            text = str(value).strip()
            if text and text not in {"0"}:
                items.append(Item(text=text, label=dataset, origin=dataset))
    # De-duplicated by text, not by value: the same task string served by two
    # datasets is one string, and counting it twice would weight a corpus by which
    # datasets happen to be on this machine.
    return _unique_by_text(items)


def hub_items(*, refresh: bool = False) -> tuple[Item, ...]:
    """Task sentences harvested from LeRobot datasets on the Hub.

    Delegates to `real_tasks`, which caches the harvest to JSON: the network here is
    slow and flaky enough that re-fetching on every run would make the experiment less
    reproducible, not more.
    """
    from experiments.clustering import real_tasks

    harvested = real_tasks.harvest(refresh=refresh)
    return _unique_by_text(
        Item(text=text, label=repo, origin=repo) for repo, text in harvested.rows
    )


def _unique_by_text(items: Iterable[Item]) -> tuple[Item, ...]:
    seen: set[str] = set()
    unique: list[Item] = []
    for item in items:
        if item.text not in seen:
            seen.add(item.text)
            unique.append(item)
    return tuple(unique)


def real_strings(*, use_hub: bool = True, refresh: bool = False) -> tuple[Item, ...]:
    """Local datasets first, then the Hub, de-duplicated, order stable."""
    items = list(local_task_strings())
    if use_hub:
        items.extend(hub_items(refresh=refresh))
    return _unique_by_text(items)


def shuffled(items: Sequence[Item], seed: int = 20260930) -> tuple[Item, ...]:
    """Arrival order. An online method is order-sensitive, so it gets a stated one."""
    pool = list(items)
    random.Random(seed).shuffle(pool)
    return tuple(pool)


def grouped(items: Sequence[Item]) -> tuple[Item, ...]:
    """The adversarial order: every member of a class arrives together.

    The friendliest possible arrival order for a leader-follower, and the reason a
    single seeded run cannot be trusted to characterise the method.
    """
    return tuple(sorted(items, key=lambda item: item.label))
