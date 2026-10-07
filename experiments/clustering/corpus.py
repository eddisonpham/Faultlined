"""A corpus big enough to measure scale, and real strings to measure it against."""

from __future__ import annotations

import csv
import itertools
import json
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

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

UNKNOWN_VERBS: tuple[str, ...] = (
    "tidy",
    "shove",
    "retrieve",
    "collect",
    "fetch",
    "declutter",
)

COLOURS: tuple[str, ...] = ("red", "blue", "green")

MODIFIERS: tuple[str, ...] = ("small", "large", "plastic", "wooden", "metal", "worn")

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
    label: str
    colour: str = ""
    verb: str = ""
    modifier: str = ""
    site: str = ""
    origin: str = "synthetic"
    unknown_verb: bool = False


def _forms(
    objects: Sequence[str],
    verbs: Sequence[str],
    *,
    modifiers: Sequence[str] = MODIFIERS,
    sites: Sequence[str] = SITES,
    unknown: bool = False,
) -> Iterator[Item]:
    """Every combination of one object, one verb, and optional colour/modifier/site."""
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
    """The control corpus: verb and colour variation only."""
    objects = [name for group in OBJECTS for name in group]
    known = list(_forms(objects, KNOWN_VERBS, modifiers=("",), sites=("",)))
    unknown = list(_forms(objects, UNKNOWN_VERBS, modifiers=("",), sites=("",), unknown=True))
    pool = known + unknown
    if target > len(pool):
        raise ValueError(f"asked for {target} strings, the grammar makes {len(pool)}")
    step = len(pool) / target
    return tuple(pool[int(index * step)] for index in range(target))


_AXIS_BASELINE: dict[str, str] = {"verb": "pick up", "colour": "", "modifier": "", "site": ""}


def axis_corpus(axis: str, values: Sequence[str]) -> tuple[Item, ...]:
    """Strings that are identical except along one axis."""
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
    """How many distinct strings the grammar can produce."""
    objects = sum(len(group) for group in OBJECTS)
    per_form = (1 + len(COLOURS)) * (1 + len(MODIFIERS)) * (1 + len(SITES))
    return objects * (len(KNOWN_VERBS) + len(UNKNOWN_VERBS)) * per_form


def synthetic_corpus(target: int = DEFAULT_TARGET) -> tuple[Item, ...]:
    """`target` distinct strings, balanced over objects, colour and verb class."""
    objects = [name for group in OBJECTS for name in group]
    known = list(_forms(objects, KNOWN_VERBS))
    unknown = list(_forms(objects, UNKNOWN_VERBS, unknown=True))
    pool = known + unknown
    if target > len(pool):
        raise ValueError(f"asked for {target} strings, the grammar makes {len(pool)}")

    step = len(pool) / target
    picked = [pool[int(index * step)] for index in range(target)]
    if len({item.text for item in picked}) != target:
        raise AssertionError("the stride produced a duplicate; the grammar is not uniform")
    return tuple(picked)


def balance(items: Sequence[Item]) -> dict[str, dict[str, int]]:
    """Counts per label, colour and verb class."""
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


def core_text(text: str) -> str:
    """Strip a task string down to the words a gold object label is made of."""
    from experiments.clustering import attributes

    masked, _matched = attributes.mask_verbs(text)
    bare, _colours = attributes.split_colour(masked)
    tokens = [word.strip(".,") for word in bare.split()]
    start = 0
    for index, token in enumerate(tokens):
        if token in {"the", "a", "an"}:
            start = index + 1
            break
    words = [word for word in tokens[start:] if word not in MODIFIERS and word != "action"]
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


def local_task_strings(root: Path = Path("var/real-data")) -> tuple[Item, ...]:
    """Task strings from LeRobot datasets already on this machine."""
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
    return _unique_by_text(items)


def hub_items(*, refresh: bool = False) -> tuple[Item, ...]:
    """Task sentences harvested from LeRobot datasets on the Hub."""
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
    """Arrival order."""
    pool = list(items)
    random.Random(seed).shuffle(pool)
    return tuple(pool)


def grouped(items: Sequence[Item]) -> tuple[Item, ...]:
    """The adversarial order: every member of a class arrives together."""
    return tuple(sorted(items, key=lambda item: item.label))
