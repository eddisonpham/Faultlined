"""Synonym-candidate ranking for the unmapped queue (ADR 0029 §5).

Clustering is demoted to this: given the novel strings and the vocabulary, say
which strings *probably* belong to which entry, and which look like one new
entry. A suggestion is a queue item, never an action - nothing here writes, and
nothing here decides.

The ranking rule is deliberately the measured one and nothing more: extracted
core equality (`extract`, EXP-2.5-08's 48/48 on the study corpus). Three cases,
each with an honest silence:

- one entry shares the core -> an **attach** candidate for it,
- no entry shares it -> a **new entry** candidate labelled with the core,
- several entries share it -> **no candidate**. Choosing between equally good
  targets is a human's call, and a ranker that guesses here is exactly the
  confident-and-wrong failure the verdict exists to avoid.

Anything looser (containment, similarity, centroids) waits for the EXP-2.5-style
candidate-precision measurement the verdict's falsifiers demand first. The
function is pure so that measurement can reuse it against labelled pairs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from data_engine.clustering.extract import extract


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked suggestion for a group of unmapped strings."""

    kind: Literal["attach", "new_entry"]
    #: The core the group shares - also the suggested label for a new entry.
    core: str
    task_strings: tuple[str, ...]
    episodes: int
    #: `attach` target. None for `new_entry`.
    entry_id: str | None = None
    entry_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "core": self.core,
            "task_strings": list(self.task_strings),
            "episodes": self.episodes,
            "entry_id": self.entry_id,
            "entry_label": self.entry_label,
        }


def candidates(
    unmapped: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
) -> list[Candidate]:
    """Rank the unmapped strings into suggestions, most fragmenting first.

    ``unmapped`` rows carry ``task_string`` and ``episodes`` (the queue's shape);
    ``entries`` carry ``id``, ``preferred_label`` and ``core``. Groups are
    processed deterministically: equal cores group together, ordering is by
    episode count then core, and the same inputs always produce the same queue.
    """
    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in unmapped:
        task = str(row.get("task_string") or "")
        core = extract(task).core
        if core:
            groups.setdefault(core, []).append(row)

    by_core: dict[str, list[Mapping[str, object]]] = {}
    for entry in entries:
        core = str(entry.get("core") or "")
        if core:
            by_core.setdefault(core, []).append(entry)

    ranked: list[Candidate] = []
    for core, rows in groups.items():
        tasks = tuple(sorted(str(row.get("task_string") or "") for row in rows))
        episodes = sum(int(cast(int, row.get("episodes") or 0)) for row in rows)
        targets = by_core.get(core, [])
        if len(targets) == 1:
            target = targets[0]
            ranked.append(
                Candidate(
                    kind="attach",
                    core=core,
                    task_strings=tasks,
                    episodes=episodes,
                    entry_id=str(target.get("id") or ""),
                    entry_label=str(target.get("preferred_label") or ""),
                )
            )
        elif not targets:
            ranked.append(
                Candidate(kind="new_entry", core=core, task_strings=tasks, episodes=episodes)
            )
        # Several entries share the core: silent on purpose (see module docstring).
    ranked.sort(key=lambda c: (-c.episodes, c.core))
    return ranked
