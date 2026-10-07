"""Run the frozen configuration over a thousand strings and measure what breaks."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

import numpy as np

from experiments.clustering import attributes, centroid_experiment, corpus, embeddings
from experiments.clustering.evaluation import metrics
from experiments.clustering.methods.online_centroids import OnlineCentroids


@dataclass(frozen=True, slots=True)
class Config:
    """One clustering configuration, identical to what `final` scored."""

    encoder: str = "minilm"
    radius: float = 0.15
    colour_weight: float = 1.0
    mask_verbs: bool = True
    rule: str = "sliding_8"
    max_clusters: int = 4096

    def describe(self) -> str:
        return (
            f"{self.encoder} r={self.radius} colour={self.colour_weight} "
            f"verbs={'masked' if self.mask_verbs else 'kept'} {self.rule}"
        )


SHIPPED_CONFIG = Config()


@dataclass(slots=True)
class Run:
    """One streaming pass, with the measurements taken during it."""

    name: str
    config: Config
    n: int
    order: str
    clusters: int
    singletons: int
    largest: int
    cohesion: float
    centroid_updates: int
    arrival_labels: list[int] = field(default_factory=list)
    frozen: int = 0
    frozen_moved: int = 0
    unfrozen_moved: int = 0
    arrivals_into_frozen: int = 0
    cluster_seconds: float = 0.0

    @property
    def capped(self) -> bool:
        """True when the cluster cap, not the data, decided the cluster count."""
        return self.clusters >= self.config.max_clusters

    @property
    def singleton_rate(self) -> float:
        return self.singletons / self.n if self.n else 0.0

    @property
    def dominance(self) -> float:
        return self.largest / self.n if self.n else 0.0

    def quality(self, gold: Sequence[str]) -> dict[str, float]:
        """Label-aware numbers."""
        if not gold:
            return {}
        return {
            "b_cubed": metrics.b_cubed(self.arrival_labels, _gold_ids(gold)),
            "fragmentation": _fragmentation(self.arrival_labels, gold),
            "intact_labels": _intact_labels(self.arrival_labels, gold),
            "impure_clusters": _impure_clusters(self.arrival_labels, gold),
        }


def score_subset(run: Run, items: Sequence[corpus.Item], origin: str) -> dict[str, float]:
    """Quality over the rows from one origin, ignoring the rest."""
    labels = [
        label
        for label, item in zip(run.arrival_labels, items, strict=True)
        if item.origin == origin
    ]
    gold = [item.label for item in items if item.origin == origin]
    if not gold:
        return {}
    return {
        "b_cubed": metrics.b_cubed(labels, _gold_ids(gold)),
        "fragmentation": _fragmentation(labels, gold),
        "intact_labels": _intact_labels(labels, gold),
        "impure_clusters": _impure_clusters(labels, gold),
    }


def _gold_ids(gold: Sequence[str]) -> list[int]:
    """Class names as consecutive integers, because the metrics are integer-keyed."""
    ids: dict[str, int] = {}
    return [ids.setdefault(name, len(ids)) for name in gold]


def _fragmentation(labels: Sequence[int], gold: Sequence[str]) -> float:
    """Mean number of clusters a gold class is spread across. 1.0 is ideal."""
    per_label: dict[str, set[int]] = {}
    for label, name in zip(labels, gold, strict=True):
        per_label.setdefault(name, set()).add(label)
    if not per_label:
        return 0.0
    return sum(len(clusters) for clusters in per_label.values()) / len(per_label)


def _intact_labels(labels: Sequence[int], gold: Sequence[str]) -> int:
    """Classes that ended up entirely inside one cluster."""
    per_label: dict[str, set[int]] = {}
    for label, name in zip(labels, gold, strict=True):
        per_label.setdefault(name, set()).add(label)
    return sum(1 for clusters in per_label.values() if len(clusters) == 1)


def _impure_clusters(labels: Sequence[int], gold: Sequence[str]) -> int:
    """Clusters holding more than one gold class."""
    per_cluster: dict[int, set[str]] = {}
    for label, name in zip(labels, gold, strict=True):
        per_cluster.setdefault(label, set()).add(name)
    return sum(1 for names in per_cluster.values() if len(names) > 1)


def encode(
    items: Sequence[corpus.Item], encoder: embeddings.Encoder, config: Config
) -> tuple[np.ndarray, float]:
    """Batch-encode through the pipeline `final` scored."""
    texts = [item.text for item in items]
    start = time.perf_counter()
    by_text = attributes.encode_for_clustering(
        encoder, texts, config.colour_weight, mask_verb=config.mask_verbs
    )
    elapsed = time.perf_counter() - start
    return np.stack([by_text[text] for text in texts]), elapsed


def stream(
    items: Sequence[corpus.Item],
    vectors: np.ndarray,
    config: Config,
    *,
    name: str,
    order: str,
    confirm_after: float = 0.5,
    confirm_top: int = 0,
) -> Run:
    """Feed every vector through the online method, recording what happened."""
    model = OnlineCentroids(
        radius=config.radius,
        max_clusters=config.max_clusters,
        rule_factory=centroid_experiment.RULES[config.rule],
    )
    labels: list[int] = []
    frozen_at: dict[int, np.ndarray] = {}
    unfrozen_at: dict[int, np.ndarray] = {}
    arrivals_into_frozen = 0
    confirm_index = int(len(items) * confirm_after)
    start = time.perf_counter()

    for position, (row, item) in enumerate(zip(vectors, items, strict=True)):
        point = np.asarray(row, dtype=np.float32)
        distances = model.distances(point)
        if distances.size and float(distances.min()) <= model.radius:
            nearest = int(np.argmin(distances))
            into_frozen = model.clusters[nearest].frozen
            model.observe(point, item.text)
            labels.append(nearest)
            if into_frozen:
                arrivals_into_frozen += 1
        else:
            before = len(model.clusters)
            model.observe(point, item.text)
            if len(model.clusters) > before:
                labels.append(before)
            else:
                labels.append(int(np.argmin(model.distances(point))))

        if confirm_top and position == confirm_index and model.clusters:
            order_by_size = sorted(
                range(len(model.clusters)), key=lambda i: -model.clusters[i].count
            )
            for index in order_by_size[:confirm_top]:
                model.confirm(index, f"synthetic c{index}")
                frozen_at[index] = model.clusters[index].centroid.copy()
            for index in range(len(model.clusters)):
                if index not in frozen_at:
                    unfrozen_at[index] = model.clusters[index].centroid.copy()

    elapsed = time.perf_counter() - start
    counts: dict[int, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    result = model.result(vectors)
    frozen_moved = sum(
        1
        for index, before in frozen_at.items()
        if index < len(model.clusters)
        and not np.array_equal(model.clusters[index].centroid, before)
    )
    unfrozen_moved = sum(
        1
        for index, before in unfrozen_at.items()
        if index < len(model.clusters)
        and not np.array_equal(model.clusters[index].centroid, before)
    )
    return Run(
        name=name,
        config=config,
        n=len(items),
        order=order,
        clusters=result.cluster_count,
        singletons=sum(1 for count in counts.values() if count == 1),
        largest=max(counts.values(), default=0),
        cohesion=result.cohesion,
        centroid_updates=result.centroid_updates,
        arrival_labels=labels,
        frozen=len(frozen_at),
        frozen_moved=frozen_moved,
        unfrozen_moved=unfrozen_moved,
        arrivals_into_frozen=arrivals_into_frozen,
        cluster_seconds=elapsed,
    )


def scale_curve(
    items: Sequence[corpus.Item],
    vectors: np.ndarray,
    config: Config,
    sizes: Sequence[int] = (50, 100, 200, 400, 800, 1200),
) -> list[Run]:
    """The same configuration over growing prefixes of one arrival order."""
    return [
        stream(items[:size], vectors[:size], config, name=f"n={size}", order="shuffled")
        for size in sizes
        if size <= len(items)
    ]


def order_sensitivity(
    items: Sequence[corpus.Item], vectors: np.ndarray, config: Config
) -> dict[str, float]:
    """How much the answer depends on the order the same strings arrive in."""
    shuffled_items = corpus.shuffled(items)
    grouped_items = corpus.grouped(items)
    position = {item.text: index for index, item in enumerate(items)}

    shuffled_run = stream(shuffled_items, vectors, config, name="shuffled", order="shuffled")
    grouped_run = stream(grouped_items, vectors, config, name="grouped", order="grouped")

    aligned = [0] * len(items)
    for label, item in zip(grouped_run.arrival_labels, grouped_items, strict=True):
        aligned[position[item.text]] = label
    return {
        "adjusted_rand": metrics.adjusted_rand(shuffled_run.arrival_labels, aligned),
        "clusters_shuffled": float(shuffled_run.clusters),
        "clusters_grouped": float(grouped_run.clusters),
        "singletons_shuffled": float(shuffled_run.singletons),
        "singletons_grouped": float(grouped_run.singletons),
    }


def sweep(
    items: Sequence[corpus.Item],
    matrices: dict[bool, np.ndarray],
    base: Config = SHIPPED_CONFIG,
    *,
    radii: Sequence[float] = (0.10, 0.15, 0.20, 0.30),
) -> list[Run]:
    """Radius and verb masking over the full corpus, from pre-encoded matrices."""
    ordered = corpus.shuffled(items)
    return [
        stream(
            ordered,
            matrices[mask],
            replace(base, radius=radius, mask_verbs=mask),
            name=f"r={radius} verbs={'masked' if mask else 'kept'}",
            order="shuffled",
        )
        for radius in radii
        for mask in (True, False)
    ]


def batch_sensitivity(
    items: Sequence[corpus.Item],
    encoder: embeddings.Encoder,
    config: Config,
    *,
    runs: int = 3,
) -> dict[str, object]:
    """How much the cluster count moves when only the batch composition changes."""
    ordered = corpus.shuffled(items)
    counts: list[float] = []
    for index in range(runs):
        rotated = ordered[index:] + ordered[:index]
        matrix, _ = encode(rotated, encoder, config)
        run = stream(rotated, matrix, config, name=f"batch {index}", order="rotated")
        counts.append(float(run.clusters))
    return {
        "clusters": counts,
        "spread": round(max(counts) - min(counts), 1),
        "relative": round((max(counts) - min(counts)) / min(counts), 4) if min(counts) else 0.0,
    }


def rule_sweep(
    items: Sequence[corpus.Item],
    vectors: np.ndarray,
    base: Config = SHIPPED_CONFIG,
    *,
    rules: Sequence[str] = (
        "running_mean",
        "ema_0.98",
        "sliding_8",
        "sliding_32",
        "robust_trim_32_0.25",
        "medoid_32",
    ),
    radii: Sequence[float] = (0.15, 0.30),
) -> list[Run]:
    """The centroid rule at scale, at both radii that matter."""
    ordered = corpus.shuffled(items)
    runs: list[Run] = []
    for radius in radii:
        for rule in rules:
            config = replace(base, radius=radius, rule=rule)
            runs.append(
                stream(ordered, vectors, config, name=f"{rule} r={radius}", order="shuffled")
            )
    return runs


ATTRIBUTES: tuple[str, ...] = ("colour", "modifier", "site", "verb")


def axis_report(items: Sequence[corpus.Item], vectors: np.ndarray) -> dict[str, dict[str, float]]:
    """Per axis: how far apart are two strings that differ *only* in that axis."""
    out: dict[str, dict[str, float]] = {}
    for axis in ("colour", "modifier", "site", "verb"):
        same: list[float] = []
        different: list[float] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                left, right = items[i], items[j]
                if any(
                    getattr(left, other) != getattr(right, other)
                    for other in ("colour", "modifier", "site", "verb")
                    if other != axis
                ):
                    continue
                distance = float(1.0 - vectors[i] @ vectors[j])
                if getattr(left, axis) == getattr(right, axis):
                    if left.label != right.label:
                        different.append(distance)
                elif left.label == right.label:
                    same.append(distance)
        if not same or not different:
            continue
        out[axis] = {
            "same_object_pairs": float(len(same)),
            "different_object_pairs": float(len(different)),
            "same_object_mean": round(float(np.mean(same)), 4),
            "different_object_mean": round(float(np.mean(different)), 4),
            "separable": bool(np.mean(same) < np.mean(different)),
            "margin": round(float(np.mean(different) - np.mean(same)), 4),
        }
    return out


def axis_isolation(
    encoder: embeddings.Encoder, config: Config = SHIPPED_CONFIG
) -> dict[str, dict[str, float]]:
    """One axis varied at a time, measured as same-object against different-object."""
    from experiments.clustering import corpus as corpus_module

    axes: dict[str, Sequence[str]] = {
        "colour": ("", *corpus_module.COLOURS),
        "modifier": ("", *corpus_module.MODIFIERS),
        "site": ("", *corpus_module.SITES),
        "verb": (*corpus_module.KNOWN_VERBS[:6], *corpus_module.UNKNOWN_VERBS[:3]),
    }
    out: dict[str, dict[str, float]] = {}
    for axis, values in axes.items():
        items = corpus_module.axis_corpus(axis, values)
        vectors, _ = encode(items, encoder, config)
        same: list[float] = []
        different: list[float] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                distance = float(1.0 - vectors[i] @ vectors[j])
                if items[i].label == items[j].label:
                    same.append(distance)
                else:
                    different.append(distance)
        same_mean = float(np.mean(same))
        different_mean = float(np.mean(different))
        out[axis] = {
            "strings": float(len(items)),
            "same_object_mean": round(same_mean, 4),
            "different_object_mean": round(different_mean, 4),
            "margin": round(different_mean - same_mean, 4),
            "separable": bool(same_mean < different_mean),
        }
    return out


def geometry_report(
    items: Sequence[corpus.Item], vectors: np.ndarray, radius: float
) -> dict[str, float]:
    """The distance geometry that decides whether any radius could work."""

    def distances(same: bool, axis: str | None) -> np.ndarray:
        rows: list[float] = []
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if (items[i].label == items[j].label) is not same:
                    continue
                if same and axis is not None and getattr(items[i], axis) == getattr(items[j], axis):
                    continue
                rows.append(float(1.0 - vectors[i] @ vectors[j]))
        return np.asarray(rows, dtype=np.float64)

    same_all = distances(True, None)
    different = distances(False, None)
    out: dict[str, float] = {
        "same_object_mean": round(float(same_all.mean()), 4),
        "different_object_mean": round(float(different.mean()), 4),
        "same_object_p90": round(float(np.percentile(same_all, 90)), 4),
        "different_object_p10": round(float(np.percentile(different, 10)), 4),
    }
    within_same = float((same_all <= radius).mean())
    within_different = float((different <= radius).mean())
    out["pairs_same_within_radius"] = round(within_same, 4)
    out["pairs_different_within_radius"] = round(within_different, 4)
    out["object_precision"] = round(
        within_same / (within_same + within_different) if within_same + within_different else 0.0,
        4,
    )
    for axis in ("modifier", "site", "verb"):
        matching = sum(
            1
            for i in range(len(items))
            for j in range(i + 1, len(items))
            if items[i].label == items[j].label
            and getattr(items[i], axis) != getattr(items[j], axis)
        )
        if matching:
            out[f"same_object_differing_{axis}_pairs"] = float(matching)
    return out


def fragmentation_by_attribute(
    run: Run, items: Sequence[corpus.Item], attribute: str
) -> dict[str, dict[str, float]]:
    """Per attribute value: how many strings, how many clusters, how concentrated."""
    buckets: dict[str, list[int]] = {}
    for label, item in zip(run.arrival_labels, items, strict=True):
        value = getattr(item, attribute) or "none"
        buckets.setdefault(value, []).append(label)
    out: dict[str, dict[str, float]] = {}
    for value, labels in buckets.items():
        counts: dict[int, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        out[value] = {
            "strings": float(len(labels)),
            "clusters": float(len(counts)),
            "modal_share": round(max(counts.values()) / len(labels), 4),
        }
    return dict(sorted(out.items(), key=lambda pair: -pair[1]["strings"]))


def subset_quality(
    run: Run, items: Sequence[corpus.Item], attribute: str, value: str
) -> dict[str, float]:
    """Quality restricted to the rows with one attribute value."""
    labels = [
        label
        for label, item in zip(run.arrival_labels, items, strict=True)
        if (getattr(item, attribute) or "none") == value
    ]
    gold = [item.label for item in items if (getattr(item, attribute) or "none") == value]
    if not gold:
        return {}
    return {
        "strings": len(gold),
        "b_cubed": round(metrics.b_cubed(labels, _gold_ids(gold)), 4),
        "fragmentation": round(_fragmentation(labels, gold), 3),
        "impure_clusters": _impure_clusters(labels, gold),
    }


def core_run(
    items: Sequence[corpus.Item],
    encoder: embeddings.Encoder,
    config: Config = SHIPPED_CONFIG,
) -> Run:
    """The same method on the extracted object core, for the headroom number."""
    core_items = corpus.core_corpus(items)
    config = replace(config, colour_weight=0.0)
    vectors, _ = encode(core_items, encoder, config)
    return stream(
        corpus.shuffled(core_items),
        vectors,
        config,
        name="core",
        order="shuffled",
    )


def summary(run: Run, gold: Sequence[str] | None = None) -> dict[str, object]:
    """One flat dict per run, so a report is written from the run and not by hand."""
    row: dict[str, object] = {
        "name": run.name,
        "order": run.order,
        "config": run.config.describe(),
        "strings": run.n,
        "clusters": run.clusters,
        "capped": run.capped,
        "singletons": run.singletons,
        "singleton_rate": round(run.singleton_rate, 4),
        "largest_cluster": run.largest,
        "dominance": round(run.dominance, 4),
        "cohesion": round(run.cohesion, 4),
        "centroid_updates": run.centroid_updates,
        "cluster_seconds": round(run.cluster_seconds, 3),
        "frozen": run.frozen,
        "frozen_centroids_moved": run.frozen_moved,
        "unfrozen_centroids_moved": run.unfrozen_moved,
        "arrivals_into_frozen": run.arrivals_into_frozen,
    }
    if gold:
        quality = run.quality(gold)
        row.update(
            {
                key: round(value, 4) if isinstance(value, float) else value
                for key, value in quality.items()
            }
        )
    return row


def coverage_note(items: Sequence[corpus.Item]) -> dict[str, object]:
    """Verb coverage over the corpus, stated as a fraction with its own denominator."""
    recognised = [
        item
        for item in items
        if not item.unknown_verb or attributes.verb_of(item.text) not in ("", "-")
    ]
    return {
        "strings": len(items),
        "verb_recognised": len(recognised),
        "coverage": round(len(recognised) / len(items), 4) if items else 0.0,
        "out_of_lexicon_verbs": sum(1 for item in items if item.unknown_verb),
    }
