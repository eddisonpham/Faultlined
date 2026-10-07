"""The hand-built gold set: 50 pairs of task strings with per-view labels."""

from __future__ import annotations

from dataclasses import dataclass

Label = bool | None


@dataclass(frozen=True, slots=True)
class GoldPair:
    """One labelled pair, scored independently per view."""

    a: str
    b: str
    same_action: Label
    same_object: Label
    note: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.same_action is None or self.same_object is None


GOLD_PAIRS: tuple[GoldPair, ...] = (
    GoldPair("pick up the red cube", "grab the red cube", True, True),
    GoldPair("place the bowl on the plate", "put the bowl on the plate", True, True),
    GoldPair("open the drawer", "pull open the drawer", True, True),
    GoldPair("close the lid", "shut the lid", True, True),
    GoldPair("wipe the table", "wipe down the table", True, True),
    GoldPair("pick up the red cube", "take the red cube", True, True),
    GoldPair("close the door", "shut the door", True, True),
    GoldPair("pick up the red cube", "pick up the blue cube", True, False),
    GoldPair("pick up the red block", "pick up the green block", True, False),
    GoldPair("place the bowl on the plate", "place the cup on the plate", True, False),
    GoldPair("push the crate", "push the trolley", True, False),
    GoldPair("open the drawer", "open the cabinet", True, False),
    GoldPair("wipe the table", "wipe the counter", True, False),
    GoldPair("insert the peg", "insert the rod", True, False),
    GoldPair("press the red button", "press the blue button", True, False),
    GoldPair("fold the shirt", "fold the pants", True, False),
    GoldPair("pour the milk", "pour the oil", True, False),
    GoldPair(
        "sort the red blocks into the bin",
        "sort the blue blocks into the bin",
        True,
        False,
        "Differs only in the object adjective; the classic over-merge trap.",
    ),
    GoldPair("pick up the red cube", "push the red cube", False, True),
    GoldPair("open the drawer", "close the drawer", False, True),
    GoldPair("place the bowl", "lift the bowl", False, True),
    GoldPair("screw in the bolt", "unscrew the bolt", False, True),
    GoldPair("fold the cloth", "unfold the cloth", False, True),
    GoldPair("press the button", "hold the button", False, True),
    GoldPair("open the bottle", "close the bottle", False, True),
    GoldPair("open the drawer", "pick up the bowl", False, False),
    GoldPair("wipe the table", "screw in the bolt", False, False),
    GoldPair("pour the water", "fold the cloth", False, False),
    GoldPair("stack the plates", "open the microwave", False, False),
    GoldPair("hand over the tool", "unscrew the bolt", False, False),
    GoldPair(
        "pick up the red cube",
        "lift the red cube",
        None,
        True,
        "'pick up' and 'lift' are usually synonyms in manipulation, but they are "
        "different primitives in some robot stacks.",
    ),
    GoldPair(
        "place the bowl",
        "put down the bowl",
        None,
        True,
        "'place' implies a target; 'put down' does not require one.",
    ),
    GoldPair(
        "push the button",
        "press the button",
        None,
        True,
        "Often the same primitive, sometimes distinct (force vs. position control).",
    ),
    GoldPair(
        "turn the knob clockwise",
        "rotate the knob clockwise",
        None,
        True,
        "'turn' and 'rotate' are near-synonyms; the question is whether the "
        "engine treats them as one skill.",
    ),
    GoldPair(
        "move the cup to the sink",
        "carry the cup to the sink",
        None,
        True,
        "'carry' implies the object is grasped; 'move' does not.",
    ),
    GoldPair(
        "lift the lid",
        "raise the lid",
        None,
        True,
        "Largely synonymous, but not identical in every robot stack.",
    ),
    GoldPair(
        "wipe the lens",
        "clean the lens",
        None,
        True,
        "Same physical outcome, possibly a different motion primitive.",
    ),
    GoldPair(
        "hand over the pen",
        "give the pen",
        None,
        True,
        "Socially the same act; robotically they may need different grasps.",
    ),
    GoldPair(
        "collect the samples",
        "gather the samples",
        None,
        True,
        "Synonyms, with no strong reason to separate them.",
    ),
    GoldPair(
        "pick up the red cube",
        "pick up the red block",
        True,
        None,
        "A 'block' and a 'cube' are often the same object under two names.",
    ),
    GoldPair(
        "pick up the bottle",
        "pick up the flask",
        True,
        None,
        "Usually the same object class, occasionally not.",
    ),
    GoldPair(
        "wipe the table",
        "wipe the workbench",
        True,
        None,
        "Different furniture in most homes, the same surface in a lab.",
    ),
    GoldPair(
        "push the cart",
        "push the cart to the left",
        True,
        None,
        "The second adds a direction. Same skill with a constraint, or a different task entirely.",
    ),
    GoldPair(
        "pick up the red cube",
        "pick up the red cube and place it on the plate",
        None,
        True,
        "The second is a compound task, not a rephrasing of the first.",
    ),
    GoldPair("push the drawer", "close the drawer", False, True),
    GoldPair("open the microwave", "close the microwave", False, True),
    GoldPair("turn the valve", "close the valve", False, True),
    GoldPair(
        "press the pedal",
        "step on the pedal",
        None,
        True,
        "Same effect, and possibly the same primitive under a different name.",
    ),
    GoldPair(
        "sweep the crumbs into the bin",
        "push the crumbs into the bin",
        None,
        True,
        "'Sweep' implies a tool; 'push' implies a hand. Different motions, same outcome.",
    ),
    GoldPair(
        "wipe the table with a cloth",
        "wipe the table",
        None,
        True,
        "The first adds the tool. A qualifier rather than a different task, "
        "but a filtering engine would treat them as different strings.",
    ),
)


def gold_for(view: str) -> tuple[GoldPair, ...]:
    """Pairs whose label for `view` is decided, i.e. not ambiguous."""
    attribute = f"same_{view}"
    return tuple(pair for pair in GOLD_PAIRS if getattr(pair, attribute) is not None)


def vocab() -> tuple[str, ...]:
    """Every distinct string in the set, sorted, for reproducibility."""
    return tuple(sorted({text for pair in GOLD_PAIRS for text in (pair.a, pair.b)}))


def summary() -> dict[str, int]:
    """Counts per view, so a report can show what it was actually scored on."""
    return {
        "pairs": len(GOLD_PAIRS),
        "distinct_strings": len(vocab()),
        "action_decided": len(gold_for("action")),
        "object_decided": len(gold_for("object")),
        "ambiguous": sum(1 for pair in GOLD_PAIRS if pair.ambiguous),
    }
