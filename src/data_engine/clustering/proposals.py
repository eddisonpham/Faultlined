"""Build cluster proposals from the task strings a catalog actually holds."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from data_engine.clustering.extract import Coverage, coverage, extract
from data_engine.clustering.lexicon import Ignored, Lexicon
from data_engine.clustering.online import OnlineCentroids, cosine, token_vector


def proposal_key(core: str) -> str:
    """Stable identity for a proposal."""
    return hashlib.blake2b(core.encode(), digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class Member:
    """One distinct task string and how many episodes carry it."""

    task: str
    core: str
    episodes: int
    verb: str = ""
    colours: tuple[str, ...] = ()
    distance: float = 0.0

    @property
    def weight(self) -> int:
        return self.episodes


@dataclass(frozen=True, slots=True)
class Proposal:
    """One suggested cluster, with everything a reviewer needs to judge it."""

    key: str
    core: str
    label: str
    frozen: bool
    members: tuple[Member, ...]
    centroid: tuple[float, ...] = ()
    merged_cores: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        """Episodes behind the proposal."""
        return sum(member.episodes for member in self.members)

    @property
    def task_count(self) -> int:
        return len(self.members)

    @property
    def is_singleton(self) -> bool:
        return self.task_count == 1

    @property
    def is_merge(self) -> bool:
        return len(self.merged_cores) > 1

    def verbs(self) -> Counter[str]:
        return Counter(member.verb or "-" for member in self.members)

    def colours(self) -> Counter[str]:
        return Counter(colour for member in self.members for colour in member.colours)


@dataclass(frozen=True, slots=True)
class ProposalSet:
    """The whole proposal run, with the numbers that say whether to trust it."""

    proposals: tuple[Proposal, ...]
    coverage: Coverage
    ignored: Ignored
    radius: float
    rule: str
    source: str
    tasks: int
    episodes: int
    confirmed: int = 0

    @property
    def cluster_count(self) -> int:
        return len(self.proposals)

    @property
    def singletons(self) -> int:
        return sum(1 for proposal in self.proposals if proposal.is_singleton)

    @property
    def merges(self) -> int:
        return sum(1 for proposal in self.proposals if proposal.is_merge)

    @property
    def frozen(self) -> int:
        return sum(1 for proposal in self.proposals if proposal.frozen)

    @property
    def dominance(self) -> float:
        total = sum(proposal.size for proposal in self.proposals)
        if not total:
            return 0.0
        largest = max((proposal.size for proposal in self.proposals), default=0)
        return largest / total

    def apply_confirmations(self, labels: Mapping[str, str]) -> int:
        """Freeze the proposals named in `labels`, keyed by proposal hash."""
        frozen = tuple(
            replace(
                proposal,
                label=labels.get(proposal.key, proposal.label),
                frozen=proposal.key in labels,
            )
            for proposal in self.proposals
        )
        object.__setattr__(self, "proposals", frozen)
        object.__setattr__(self, "confirmed", sum(1 for item in frozen if item.frozen))
        return self.confirmed

    def ordered(self) -> tuple[Proposal, ...]:
        """Biggest first: the proposals that matter are the ones with the most episodes."""
        return tuple(sorted(self.proposals, key=lambda item: (-item.size, item.core)))

    def health(self) -> dict[str, float | int | str]:
        """The block of numbers the page leads with."""
        return {
            "tasks": self.tasks,
            "episodes": self.episodes,
            "clusters": self.cluster_count,
            "singletons": self.singletons,
            "merges": self.merges,
            "frozen": self.frozen,
            "dominance": round(self.dominance, 4),
            "distinct_cores": self.coverage.distinct_cores,
            "verb_rate": round(self.coverage.verb_rate, 4),
            "single_token_rate": round(self.coverage.single_token_rate, 4),
            "ignored": self.ignored.describe(),
            "radius": self.radius,
            "rule": self.rule,
            "source": self.source,
        }


@dataclass(slots=True)
class _Bucket:
    members: list[Member] = field(default_factory=list)
    cores: list[str] = field(default_factory=list)


def build(
    tasks: Mapping[str, int],
    *,
    lexicon: Lexicon | None = None,
    ignored: Ignored | None = None,
    radius: float = 0.30,
    rule: str = "running_mean",
    source: str = "catalog",
    order: Sequence[str] | None = None,
) -> ProposalSet:
    """Group `(task string -> episode count)` into proposals."""
    lexicon = lexicon or Lexicon()
    ignored = ignored or Ignored()
    sequence = list(order) if order is not None else sorted(tasks)
    extractions = {task: extract(task, lexicon, ignored=ignored) for task in sequence}
    measured = coverage([extractions[task] for task in sequence])

    model = OnlineCentroids(radius=radius, rule=rule)
    for task in sequence:
        item = extractions[task]
        if not item.usable:
            continue
        model.observe(item.core)

    clusters = model.by_core()
    final_distances = {
        core: 1.0 - cosine(model.centroid_of(index), token_vector(core))
        for core, index in clusters.items()
    }
    buckets: dict[int, _Bucket] = {}
    for core, index in clusters.items():
        bucket = buckets.setdefault(index, _Bucket())
        bucket.cores.append(core)

    for task in sequence:
        item = extractions[task]
        if not item.usable:
            continue
        bucket = buckets[clusters[item.core]]
        bucket.members.append(
            Member(
                task=task,
                core=item.core,
                episodes=int(tasks[task]),
                verb=item.verb,
                colours=item.colours,
                distance=final_distances[item.core],
            )
        )

    proposals: list[Proposal] = []
    for index, bucket in buckets.items():
        primary = max(bucket.cores, key=lambda core: _weight(bucket.members, core))
        proposals.append(
            Proposal(
                key=proposal_key(primary),
                core=primary,
                label="",
                frozen=False,
                members=tuple(sorted(bucket.members, key=lambda m: (-m.episodes, m.task))),
                centroid=tuple(model.centroid_of(index)),
                merged_cores=tuple(sorted(bucket.cores)),
            )
        )

    return ProposalSet(
        proposals=tuple(proposals),
        coverage=measured,
        ignored=ignored,
        radius=radius,
        rule=rule,
        source=source,
        tasks=len(sequence),
        episodes=sum(int(count) for count in tasks.values()),
    )


def _weight(members: Sequence[Member], core: str) -> int:
    return sum(member.episodes for member in members if member.core == core)


def sample_tasks() -> dict[str, int]:
    """Task strings to propose over when the catalog has nothing to offer."""
    from data_engine.clustering import sample_data

    return sample_data.sample_tasks()


def from_texts(texts: Iterable[str]) -> dict[str, int]:
    """One episode per distinct string, for callers with no counts to supply."""
    counter: Counter[str] = Counter()
    for text in texts:
        cleaned = text.strip()
        if cleaned:
            counter[cleaned] += 1
    return dict(counter)
