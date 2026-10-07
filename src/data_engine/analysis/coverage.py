"""What a build contains, and what it lacks (ADR 0033).

The industry measures dataset diversity once, offline, in a paper: the AgiBot/Shanghai study
(arXiv:2507.06219) decomposes it into task, embodiment and expert and spends its budget separating
them; OXE and DROID publish counts; dataset cards and Croissant carry a publisher's *claim*; Dataset
Cartography needs a model trained several times over the data. None of them answers the question an
operator has in front of a build they are about to export: **what does this contain, and what does
it lack?**

Here the three axes that study separated are columns the catalog already holds. A task string
normalised against a human-approved vocabulary (ADR 0029), a robot, a format, and a quality verdict
are all persisted per episode at ingest, so coverage is an aggregation of rows rather than a study.
This module is the pure half - the aggregation query lives in the catalog and the surface in the API
and the build page, exactly as ADR 0032 split the fingerprint.

Four properties are deliberate, and they are the reason this is a report and not a score:

- **Gaps, not a number.** A diversity index ("this build is 0.62 diverse") cannot be acted on. The
  actionable object is a set difference: the values the reference set holds and the build does not.
  This is the one thing none of the surveyed systems publishes.
- **Two references, and both are reported.** The *catalog* ("you curated out every episode of the
  other robot") and, for tasks, the operator's own *vocabulary* ("you named `fold the cloth` and
  there is no episode for it anywhere"). The second is why task gaps come from the vocabulary rather
  than from the episodes: a gap has to be expressible before it has been filled.
- **Bounded by construction.** `VALUE_LIMIT` values per axis and `GAP_LIMIT` gaps per axis, each
  with an explicit truncation flag and the true total, so the payload is bounded by the axes
  rather than by the episode count. `quality/summary` already ships 2.2 MiB at 10k episodes
  (EXP-0015) and this surface is not allowed to become the same problem.
- **It advises; it does not gate.** No coverage figure enters a build's identity and no build is
  refused for a gap. A build missing a task is a fact about the build; whether that matters belongs
  to the operator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "AXES",
    "GAP_LIMIT",
    "VALUE_LIMIT",
    "CoverageAxis",
    "CoverageReport",
    "CoverageValue",
    "coverage_report",
]

#: The most distinct values one axis reports in a build's distribution. A build whose task list is
#: longer than this is summarised rather than dumped, and says so.
VALUE_LIMIT = 20

#: The most gaps one axis reports. The full count travels beside the list, so a truncated gap list
#: reads as "at least this many" rather than as the whole answer.
GAP_LIMIT = 20

#: The axes, in report order, with the label the surface uses. Every one is an attribute the catalog
#: already persists; ADR 0033 decision 2 forbids deriving a new one here.
AXES: tuple[tuple[str, str], ...] = (
    ("task", "Task"),
    ("robot", "Embodiment"),
    ("format", "Format"),
    ("verdict", "Verdict"),
)

#: The axis whose gaps come from the operator's vocabulary instead of from the catalog's values
#: (ADR 0033 decision 4). Every other axis reports the catalog's values that the build left out.
_VOCABULARY_AXIS = "task"


@dataclass(frozen=True, slots=True)
class CoverageValue:
    """One value on one axis, with how much of the build it accounts for."""

    value: str
    count: int
    share: float
    """`count / episode_count`, or 0.0 for an empty build. Reported so a large build and a small one
    read comparably, and so a value that is 1 of 400 is visibly not 1 of 2."""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "count": self.count, "share": self.share}


@dataclass(frozen=True, slots=True)
class CoverageAxis:
    """One axis of a build's coverage: what it holds, and what the reference set holds instead."""

    name: str
    label: str
    present: int
    """Distinct values the build holds, before `VALUE_LIMIT` truncation. Zero for an empty build."""

    values: tuple[CoverageValue, ...]
    gaps: tuple[str, ...]
    """Values the reference set holds and the build does not, alphabetical, truncated to
    `GAP_LIMIT`. For the task axis these are vocabulary labels, so a label with no episode anywhere
    is a gap here (ADR 0033 decision 4)."""

    missing: int
    """How many gaps there are before truncation, so a capped list says "at least"."""

    truncated: bool
    """Whether either the distribution or the gap list was capped."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.name,
            "label": self.label,
            "present": self.present,
            "values": [item.to_dict() for item in self.values],
            "gaps": list(self.gaps),
            "missing": self.missing,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What one build contains, measured against the catalog it was drawn from (ADR 0033).

    Read-only by construction, and it says so: nothing here is removed from the build, nothing is
    refused, and no coverage figure takes part in the build's identity.
    """

    build_hash: str
    episode_count: int
    catalog_size: int
    axes: tuple[CoverageAxis, ...]
    vocabulary_total: int
    """Entries in the task vocabulary, whether or not an episode exists for them."""

    vocabulary_missing: int
    """Entries with no episode in this build."""

    @property
    def coverage_ratio(self) -> float:
        """Share of the catalog's episode population this build accounts for.

        Not a diversity measure and not claimed as one: it is the one scalar here that is a ratio of
        two counted things, and it is what makes an empty build obviously empty.
        """
        if not self.catalog_size:
            return 0.0
        return self.episode_count / self.catalog_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "build-coverage-v1",
            "build_hash": self.build_hash,
            "episode_count": self.episode_count,
            "catalog_size": self.catalog_size,
            "coverage_ratio": self.coverage_ratio,
            "axes": [axis.to_dict() for axis in self.axes],
            "vocabulary_total": self.vocabulary_total,
            "vocabulary_missing": self.vocabulary_missing,
        }

    def axis(self, name: str) -> CoverageAxis | None:
        for item in self.axes:
            if item.name == name:
                return item
        return None


def coverage_report(
    build_hash: str,
    inputs: Mapping[str, Any],
    *,
    value_limit: int = VALUE_LIMIT,
    gap_limit: int = GAP_LIMIT,
) -> CoverageReport:
    """Fold one `build_coverage_inputs` result into the report a surface renders.

    Never raises on a malformed input: a missing or non-numeric count is read as zero, so a
    broken query result degrades to a report that says it has nothing rather than to a 500. The
    alternative - propagating the failure - would make the coverage section of a build page dark
    for a reason the operator cannot see, which is the failure ADR 0023 prevents a layer down.
    """
    episode_count = _count(inputs.get("episode_count"))
    catalog_size = _count(inputs.get("catalog_size"))
    rows = _rows(inputs.get("values"))
    vocabulary = inputs.get("vocabulary")
    vocabulary = vocabulary if isinstance(vocabulary, Mapping) else {}
    gaps_by_axis = _vocabulary_gaps(vocabulary, gap_limit)

    axes: list[CoverageAxis] = []
    for name, label in AXES:
        axis_rows = [row for row in rows if row.get("axis") == name]
        values = tuple(
            CoverageValue(
                value=str(row.get("value") or ""),
                count=_count(row.get("build_count")),
                share=_share(_count(row.get("build_count")), episode_count),
            )
            for row in _ordered(axis_rows, value_limit)
        )
        if name == _VOCABULARY_AXIS:
            gaps, missing = gaps_by_axis
            truncated = len(gaps) < missing or len(values) < _distinct(axis_rows, "build_distinct")
        else:
            gaps, missing = _catalog_gaps(axis_rows, gap_limit)
            truncated = len(gaps) < missing or len(values) < _distinct(axis_rows, "build_distinct")
        axes.append(
            CoverageAxis(
                name=name,
                label=label,
                present=_distinct(axis_rows, "build_distinct"),
                values=values,
                gaps=gaps,
                missing=missing,
                truncated=truncated,
            )
        )

    return CoverageReport(
        build_hash=build_hash,
        episode_count=episode_count,
        catalog_size=catalog_size,
        axes=tuple(axes),
        vocabulary_total=_count(vocabulary.get("total")),
        vocabulary_missing=_count(vocabulary.get("missing_total")) or gaps_by_axis[1],
    )


def _ordered(rows: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    """The build's distribution: present values only, most-common first, ties by value.

    The order is part of the report rather than an accident of the query planner, because two runs
    over one catalog have to produce the same bytes for the report to be citable the way a build
    hash is. The query already ranks this way; re-sorting here means a caller that hands rows over
    in any order still gets the same report.
    """
    present = [row for row in rows if _count(row.get("build_count")) > 0]
    present.sort(key=lambda row: (-_count(row.get("build_count")), str(row.get("value") or "")))
    return present[: max(limit, 0)]


def _catalog_gaps(rows: Sequence[Mapping[str, Any]], limit: int) -> tuple[tuple[str, ...], int]:
    """Values the catalog holds and the build does not, and how many there were before the cap.

    The total comes from the query's window count, not from the rows handed over: the query caps the
    rows it returns, so counting them would report "no gaps" for the axes whose gap list was the one
    that got cut - a silent under-report, which is the failure this whole surface exists to avoid.
    """
    gaps = sorted(
        str(row.get("value") or "")
        for row in rows
        if _count(row.get("build_count")) == 0 and _count(row.get("catalog_count")) > 0
    )
    return tuple(gaps[: max(limit, 0)]), max(_distinct(rows, "gap_total"), len(gaps))


def _vocabulary_gaps(vocabulary: Mapping[str, Any], limit: int) -> tuple[tuple[str, ...], int]:
    """The task axis's gaps: vocabulary entries with no episode in this build.

    Read from the vocabulary rather than from the episodes because an entry with no episode anywhere
    is still a gap, and because it bounds the list by what the operator has named rather than by
    what happens to be stored.
    """
    entries = vocabulary.get("gaps")
    labels = [
        str(entry.get("label") or "")
        for entry in (entries if isinstance(entries, Sequence) else [])
        if isinstance(entry, Mapping)
    ]
    labels.sort()
    missing = _count(vocabulary.get("missing_total"))
    return tuple(labels[: max(limit, 0)]), max(missing, len(labels))


def _distinct(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    """One axis's total before the cap, taken from the query's window count."""
    for row in rows:
        value = _count(row.get(key))
        if value:
            return value
    return 0


def _rows(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Sequence):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _count(value: Any) -> int:
    """A count, or 0 for anything that is not a finite number (ADR 0023's rule, one layer down).

    `Decimal` is in the list because that is what PostgreSQL returns for `sum()` over a `bigint`,
    which is how the first run of this feature reported every total as zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return 0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return int(number)


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0
