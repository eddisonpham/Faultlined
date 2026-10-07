"""Entry point for the stage 2.5 clustering experiments.

Run with `just cluster <subcommand>`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from experiments.clustering import attributes, embeddings
from experiments.clustering.evaluation import gold, splits
from experiments.clustering.evaluation.metrics import PairScores

RESULTS = Path(__file__).parent / "results"


def _write(name: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


GOLD_SETS = ("hand", "constructed")


def sep_metrics() -> tuple[str, ...]:
    from experiments.clustering.evaluation.separability import METRICS

    return METRICS


def _gold_for(name: str) -> tuple[gold.GoldPair, ...]:
    if name == "hand":
        return gold.GOLD_PAIRS
    from experiments.clustering.evaluation import gold_grid

    return gold_grid.as_gold_pairs(gold_grid.constructed_pairs())


def _vocab_for(name: str) -> tuple[str, ...]:
    if name == "hand":
        return gold.vocab()
    from experiments.clustering.evaluation import gold_grid

    return gold_grid.vocab(gold_grid.constructed_pairs())


def _split_for(name: str) -> splits.Split:
    if name == "hand":
        return splits.split()
    from experiments.clustering.evaluation import gold_grid

    result: splits.Split = gold_grid.split(gold_grid.constructed_pairs())[2]
    return result


def cmd_gold(args: argparse.Namespace) -> int:
    """Report a gold set and the leakage report for its dev/held-out split."""
    result = _split_for(args.set_name)
    pairs = _gold_for(args.set_name)
    from experiments.clustering.evaluation import gold_grid

    summary = (
        gold.summary()
        if args.set_name == "hand"
        else gold_grid.summary(gold_grid.constructed_pairs())
    )
    payload = {
        "experiment": "gold",
        "set": args.set_name,
        "summary": summary,
        "split": result.report(),
        "ambiguous_pairs": [
            {"a": pair.a, "b": pair.b, "note": pair.note} for pair in pairs if pair.ambiguous
        ],
    }
    path = _write("gold" if args.set_name == "hand" else f"gold-{args.set_name}", payload)
    print(json.dumps({k: payload[k] for k in ("summary", "split")}, indent=2))
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    if not result.clean:
        print("\nFAILED: the split leaks; no result from this harness is trustworthy")
        return 1
    return 0


RADIUS_GRID = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40)


def _score_on_dev(
    rule: str,
    encoder: embeddings.Encoder,
    texts: Sequence[str],
    radius: float,
    scorer: Callable[[Sequence[int], Sequence[str], str], PairScores],
) -> tuple[float, float]:
    """Pair F1 and over-merge rate for one rule at one radius, on dev strings."""
    from experiments.clustering import centroid_experiment
    from experiments.clustering.methods.online_centroids import OnlineCentroids

    vectors = embeddings.encode_all(encoder, texts)
    model = OnlineCentroids(radius=radius, rule_factory=centroid_experiment.RULES[rule])
    result = model.fit(vectors, texts)
    scores = scorer(result.labels, texts, rule)
    return scores.pair_f1, scores.over_merge_rate


def cmd_centroids(args: argparse.Namespace) -> int:
    """Decide the centroid rule on dev data, before the factorial."""
    from experiments.clustering import centroid_experiment
    from experiments.clustering.evaluation import evaluation as ev

    encoder = embeddings.get(args.embedding)
    if not embeddings.is_semantic(encoder):
        print(
            "refusing to decide a centroid rule on the null backend: it has no "
            "semantic structure for robustness to preserve"
        )
        return 1

    split = _split_for(args.set_name)
    texts = sorted({t for pair in split.dev for t in (pair.a, pair.b)})
    scorer = ev.pair_scorer(split.dev, args.view)

    chosen: dict[str, float] = {}
    for rule in centroid_experiment.RULES:
        scored = []
        for radius in RADIUS_GRID:
            f1, over = _score_on_dev(rule, encoder, texts, radius, scorer)
            scored.append((over, -f1, radius, f1))
        best = min(scored)
        chosen[rule] = best[2]

    outcomes = []
    for rule in centroid_experiment.RULES:
        outcomes.append(
            centroid_experiment.run_rule(rule, encoder, texts, radius=chosen[rule], scorer=scorer)
        )

    ranked = sorted(outcomes, key=lambda o: (o.drift, o.outlier_pull))
    payload = {
        "experiment": "centroids",
        "embedding": embeddings.describe(encoder),
        "gold_set": args.set_name,
        "view": args.view,
        "radii": {rule: chosen[rule] for rule in sorted(chosen)},
        "split": "dev",
        "inputs": len(texts),
        "outcomes": [o.as_dict() for o in ranked],
    }
    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    path = _write(f"centroids-{encoder.name}-{args.view}{suffix}", payload)

    print(
        f"embedding {encoder.name} dim={encoder.dim}  inputs={len(texts)}  "
        f"view={args.view}  gold={args.set_name}"
    )
    print(
        f"{'rule':22s} {'radius':>7s} {'drift':>7s} {'pull':>7s} {'recov':>7s} "
        f"{'us/pt':>7s} {'mem':>4s} {'k':>3s} {'kvar':>5s} {'pairF1':>7s} {'over':>6s}"
    )
    for o in ranked:
        print(
            f"{o.rule:22s} {chosen[o.rule]:7.2f} {o.drift:7.4f} {o.outlier_pull:7.4f} "
            f"{o.recovery:7.3f} {o.micros_per_point:7.1f} {o.points_retained:4d} "
            f"{o.cluster_count:3d} {o.distinct_counts:5d} {o.pair_f1:7.3f} "
            f"{o.over_merge_rate:6.3f}"
        )
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


def cmd_projection(args: argparse.Namespace) -> int:
    """Does the learned projection make the action view work by threshold alone?"""
    from experiments.clustering import projection
    from experiments.clustering.evaluation import separability as sep

    encoder = embeddings.get(args.embedding)
    check = embeddings.verify(encoder)
    if not check.ok:
        print(f"refusing to report a result for {encoder.name}: verification failed")
        return 1

    texts = sorted(_vocab_for(args.set_name))
    vectors = embeddings.encode_all(encoder, texts)
    by_text = sep.all_vectors(vectors, texts)
    split = _split_for(args.set_name)

    payload: dict[str, Any] = {
        "experiment": "projection",
        "embedding": embeddings.describe(encoder),
        "gold_set": args.set_name,
        "split": split.report(),
        "views": {},
    }
    print(f"embedding {encoder.name} dim={encoder.dim}  gold={args.set_name}")
    print(f"{'view':7s} {'k':>3s} {'dev bacc':>9s} {'dev over':>9s} {'dev split':>9s}")
    for view in ("action", "object"):
        shrinkage = args.shrinkage if args.shrinkage is not None else projection.DEFAULT_SHRINKAGE
        fitted, sweep = projection.choose_k(
            by_text, split.dev, view, encoder.name, shrinkage, metric=args.metric
        )
        projected = fitted.project(by_text)
        probe_before = sep.probes(by_text, split.dev, split.heldout, split.dev_pair_groups)[view]
        probe_after = sep.probes(projected, split.dev, split.heldout, split.dev_pair_groups)[view]

        metrics: dict[str, Any] = {}
        for metric in ("cosine", "euclidean"):
            dev_before = sep.separability(by_text, split.dev, metric=metric)[view]
            dev_after = sep.separability(projected, split.dev, metric=metric)[view]
            before = sep.separability(by_text, split.heldout, metric=metric)[view]
            after = sep.separability(projected, split.heldout, metric=metric)[view]
            metrics[metric] = {
                "dev_before": dev_before.as_dict(),
                "dev_after": dev_after.as_dict(),
                "before": before.as_dict(),
                "after": after.as_dict(),
            }

        payload["views"][view] = {
            "metric": args.metric,
            "shrinkage": shrinkage,
            "sweep": [row.as_dict() for row in sweep],
            "chosen": fitted.as_dict(),
            "heldout": metrics,
            "probe_before": probe_before.as_dict(),
            "probe_after": probe_after.as_dict(),
        }
        for row in sweep:
            print(
                f"{view:7s} {row.k:3d} {row.dev_balanced_accuracy:9.3f} "
                f"{row.dev_false_merges:9d} {row.dev_false_splits:9d}"
            )
        for metric, values in metrics.items():
            before, after = values["before"], values["after"]
            dev_before, dev_after = values["dev_before"], values["dev_after"]
            print(
                f"  {view} [{metric:9s}] dev {dev_before['best_accuracy']:.3f} -> "
                f"{dev_after['best_accuracy']:.3f} | held-out "
                f"{before['best_accuracy']:.3f} -> {after['best_accuracy']:.3f} "
                f"(false merges {before['false_merges']} -> {after['false_merges']})"
            )
        print(
            f"  {view} [probe    ] held-out "
            f"{probe_before.heldout_balanced_accuracy:.3f} -> "
            f"{probe_after.heldout_balanced_accuracy:.3f}"
        )

    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    path = _write(f"projection-{encoder.name}{suffix}", payload)
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


BOUNDARY_MARGIN = 1.5


COLOUR_WEIGHTS = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)


def cmd_colours(args: argparse.Namespace) -> int:
    """Choose the colour-facet weight on dev, sweeping the radius with it."""
    from experiments.clustering import attributes, centroid_experiment
    from experiments.clustering.evaluation import evaluation as ev
    from experiments.clustering.methods.online_centroids import OnlineCentroids

    encoder = embeddings.get(args.embedding)
    if not embeddings.is_semantic(encoder):
        print("refusing to fit a colour facet on the null backend: no semantics to offset")
        return 1

    split = _split_for(args.set_name)
    texts = sorted({t for pair in split.dev for t in (pair.a, pair.b)})
    scorer = ev.pair_scorer(split.dev, args.view)

    rows: list[tuple[float, float, float, float, float, int]] = []
    for weight in COLOUR_WEIGHTS:
        by_text = attributes.encode_for_clustering(
            encoder, texts, weight, mask_verb=args.mask_verbs
        )
        matrix = np.stack([by_text[text] for text in texts])
        for radius in RADIUS_GRID:
            model = OnlineCentroids(
                radius=radius, rule_factory=centroid_experiment.RULES[args.rule]
            )
            result = model.fit(matrix, texts)
            scores = scorer(result.labels, texts, args.rule)
            rows.append(
                (
                    scores.over_merge_rate,
                    -scores.pair_f1,
                    weight,
                    radius,
                    scores.pair_f1,
                    len(scores.over_merged),
                )
            )

    best = min(rows)
    payload = {
        "experiment": "colours",
        "embedding": embeddings.describe(encoder),
        "gold_set": args.set_name,
        "view": args.view,
        "rule": args.rule,
        "chosen": {"weight": best[2], "radius": best[3]},
        "sweep": [
            {
                "weight": row[2],
                "radius": row[3],
                "dev_pair_f1": row[4],
                "dev_false_merges": row[5],
            }
            for row in rows
        ],
        "split": "dev",
    }
    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    path = _write(f"colours-{encoder.name}-{args.view}{suffix}", payload)

    print(f"embedding {encoder.name}  view={args.view}  gold={args.set_name}  rule={args.rule}")
    print(f"{'weight':>7s} {'radius':>7s} {'dev F1':>8s} {'dev over':>9s}")
    for row in sorted(rows, key=lambda item: (item[2], item[3])):
        marker = "  <-" if (row[2], row[3]) == (best[2], best[3]) else ""
        print(f"{row[2]:7.2f} {row[3]:7.2f} {row[4]:8.3f} {row[5]:9d}{marker}")
    print(f"\nchosen: colour weight {best[2]:g}, radius {best[3]:g}")
    print(f"wrote {path.relative_to(Path.cwd())}")
    return 0


def _frozen_fit(
    args: argparse.Namespace,
) -> tuple[
    embeddings.Encoder,
    splits.Split,
    dict[str, np.ndarray],
    list[str],
    np.ndarray,
    Any,
    Any,
    PairScores,
]:
    """Fit the frozen configuration on held-out strings and return the pieces."""
    from experiments.clustering import centroid_experiment
    from experiments.clustering.evaluation import evaluation as ev
    from experiments.clustering.methods.online_centroids import OnlineCentroids

    encoder = embeddings.get(args.embedding)
    split = _split_for(args.set_name)
    vocabulary = sorted(_vocab_for(args.set_name))
    by_text = attributes.encode_for_clustering(
        encoder, vocabulary, args.colour_weight, mask_verb=args.mask_verbs
    )
    texts = sorted({t for pair in split.heldout for t in (pair.a, pair.b)})
    vectors = np.stack([by_text[text] for text in texts])
    model = OnlineCentroids(radius=args.radius, rule_factory=centroid_experiment.RULES[args.rule])
    result = model.fit(vectors, texts)
    scores = ev.pair_scorer(split.heldout, args.view)(result.labels, texts, args.rule)
    return encoder, split, by_text, texts, vectors, model, result, scores


def cmd_plot(args: argparse.Namespace) -> int:
    """Render the frozen clustering as three reviewable SVG figures."""
    from experiments.clustering import figures
    from experiments.clustering.evaluation import gold_grid

    encoder = embeddings.get(args.embedding)
    check = embeddings.verify(encoder)
    if not check.ok:
        print(f"refusing to draw {encoder.name}: verification failed")
        return 1

    _encoder, split, _by_text, texts, vectors, _model, result, scores = _frozen_fit(args)
    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    stem = f"{encoder.name}-{args.view}{suffix}"

    members: dict[int, list[str]] = {}
    for label, text in zip(result.labels, texts, strict=True):
        members.setdefault(int(label), []).append(text)
    ordered = sorted(members)
    sizes = [len(members[label]) for label in ordered]
    row_labels = [f"c{label}" for label in ordered]

    tags: dict[str, list[str]] = {}
    for text in texts:
        colour = attributes.colour_of(text)
        tags[text] = [f"colour:{colour}"]
    if args.set_name == "constructed":
        for text, (verb, _object) in gold_grid.string_classes(
            gold_grid.constructed_pairs()
        ).items():
            if text in tags:
                tags[text].append(f"verb:{verb}")

    columns = sorted({tag for text in texts for tag in tags[text]})
    matrix = figures.cluster_attribute_matrix(members, tags, columns)
    top_verbs = sorted(column for column in columns if column.startswith("verb:"))

    FIGURES = "figures"
    out = RESULTS / FIGURES
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    svg = figures.render_treemap(sizes, row_labels, f"object clusters ({args.set_name} held-out)")
    (out / f"treemap-{stem}.svg").write_text(svg, encoding="utf-8")
    written.append(f"treemap-{stem}.svg")

    svg = figures.render_map(vectors, result.labels, f"object clusters ({args.set_name} held-out)")
    (out / f"map-{stem}.svg").write_text(svg, encoding="utf-8")
    written.append(f"map-{stem}.svg")

    heat_columns = [column for column in columns if not column.startswith("verb:")] + top_verbs
    heat = figures.render_heatmap(
        matrix[:, [columns.index(column) for column in heat_columns]],
        row_labels,
        heat_columns,
        f"cluster composition ({args.set_name} held-out)",
    )
    (out / f"heatmap-{stem}.svg").write_text(heat, encoding="utf-8")
    written.append(f"heatmap-{stem}.svg")

    print(
        f"configuration: {encoder.name} / {args.view} / {args.rule} / radius {args.radius} "
        f"/ colour weight {args.colour_weight}"
    )
    print(
        f"held-out: {len(split.heldout)} pairs, {len(texts)} strings, "
        f"{result.cluster_count} clusters, F1 {scores.pair_f1:.3f}, "
        f"{len(scores.over_merged)} false merges"
    )
    print(f"largest cluster {max(sizes)}, smallest {min(sizes)}")
    for name in written:
        print(f"wrote {(out / name).relative_to(Path.cwd())}")
    return 0


def cmd_final(args: argparse.Namespace) -> int:
    """Score the frozen configuration on held-out pairs, once, and audit its edges."""
    from experiments.clustering import centroid_experiment
    from experiments.clustering.evaluation import evaluation as ev
    from experiments.clustering.methods.online_centroids import OnlineCentroids

    encoder = embeddings.get(args.embedding)
    check = embeddings.verify(encoder)
    if not check.ok:
        print(f"refusing to score {encoder.name}: verification failed")
        return 1

    split = _split_for(args.set_name)
    vocabulary = sorted(_vocab_for(args.set_name))
    by_text = attributes.encode_for_clustering(
        encoder, vocabulary, args.colour_weight, mask_verb=args.mask_verbs
    )

    heldout_texts = sorted({t for pair in split.heldout for t in (pair.a, pair.b)})
    heldout_vectors = np.stack([by_text[text] for text in heldout_texts])

    model = OnlineCentroids(radius=args.radius, rule_factory=centroid_experiment.RULES[args.rule])
    result = model.fit(heldout_vectors, heldout_texts)
    scores = ev.pair_scorer(split.heldout, args.view)(result.labels, heldout_texts, args.rule)

    verdicts: dict[str, list[str]] = {}
    for pair in split.heldout:
        verdict = getattr(pair, f"same_{args.view}")
        if verdict is None:
            continue
        verdict_text = "same" if verdict else "different"
        verdicts.setdefault(pair.a, []).append(f"{verdict_text} as {pair.b}")
        verdicts.setdefault(pair.b, []).append(f"{verdict_text} as {pair.a}")

    members: dict[int, list[str]] = {}
    for label, text in zip(result.labels, heldout_texts, strict=True):
        members.setdefault(int(label), []).append(text)

    centroid_of: dict[str, int] = {
        text: int(label) for label, text in zip(result.labels, heldout_texts, strict=True)
    }
    centroids = np.stack([c.centroid for c in model.clusters])

    boundary: list[dict[str, Any]] = []
    for text in heldout_texts:
        label = centroid_of[text]
        point = by_text[text]
        centre = centroids[label]
        cosine = float(point @ centre) / (
            float(np.linalg.norm(point)) * float(np.linalg.norm(centre))
        )
        distance = 1.0 - cosine
        if distance > args.radius * BOUNDARY_MARGIN:
            continue
        peers = [peer for peer in members[label] if peer != text]
        boundary.append(
            {
                "text": text,
                "cluster": label,
                "distance_to_centroid": round(distance, 4),
                "radius": args.radius,
                "merged_with": peers[:3],
                "cluster_size": len(peers) + 1,
                "gold_says": verdicts.get(text, [])[:3],
            }
        )
    boundary.sort(key=lambda row: -float(row["distance_to_centroid"]))

    payload = {
        "experiment": "final",
        "configuration": {
            "embedding": embeddings.describe(encoder),
            "view": args.view,
            "gold_set": args.set_name,
            "rule": args.rule,
            "radius": args.radius,
            "colour_weight": args.colour_weight,
            "mask_verbs": args.mask_verbs,
            "verb_mask_coverage": (
                attributes.verb_mask_coverage(heldout_texts) if args.mask_verbs else None
            ),
            "frozen_before_scoring": True,
        },
        "split": split.report(),
        "heldout": {
            "pairs": len(split.heldout),
            "strings": len(heldout_texts),
            "clusters": result.cluster_count,
            "pair_f1": scores.pair_f1,
            "over_merge_rate": scores.over_merge_rate,
            "under_merge_rate": scores.under_merge_rate,
            "false_merges": len(scores.over_merged),
            "false_splits": len(scores.under_merged),
            "unassigned": scores.unassigned,
            "over_merged_pairs": [{"a": left, "b": right} for left, right in scores.over_merged],
            "under_merged_pairs": [{"a": left, "b": right} for left, right in scores.under_merged],
        },
        "boundary_cases": boundary,
    }
    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    path = _write(f"final-{encoder.name}-{args.view}{suffix}", payload)

    print(f"configuration: {encoder.name} / {args.view} / {args.rule} / radius {args.radius}")
    print(f"gold set: {args.set_name}   held-out pairs: {len(split.heldout)}")
    print(
        f"clusters: {result.cluster_count}  pair F1: {scores.pair_f1:.3f}  "
        f"over-merge: {scores.over_merge_rate:.3f} ({len(scores.over_merged)})  "
        f"under-merge: {scores.under_merge_rate:.3f} ({len(scores.under_merged)})  "
        f"unassigned: {scores.unassigned}"
    )
    print(f"\nboundary cases within {BOUNDARY_MARGIN}x the radius: {len(boundary)}")
    if args.mask_verbs:
        unmatched = [t for t in heldout_texts if not attributes.mask_verbs(t)[1]]
        print(
            f"verb masking: {attributes.verb_mask_coverage(heldout_texts):.0%} of "
            f"{len(heldout_texts)} strings matched a known verb"
        )
        for text in unmatched[:5]:
            print(f"  UNMASKED: {text!r}")
    for left, right in scores.over_merged:
        print(f"  FALSE MERGE: {left!r} was clustered with {right!r} (same {args.view}: False)")
    for row in boundary[:12]:
        print(f"  {row['text']!r}  d={row['distance_to_centroid']}  -> {row['merged_with']}")
        print(f"      gold: {row['gold_says']}")
    if len(boundary) > 12:
        print(f"  ... {len(boundary) - 12} more in {path.relative_to(Path.cwd())}")
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


def sep_all(vectors: Any, texts: Sequence[str]) -> dict[str, Any]:
    from experiments.clustering.evaluation.separability import all_vectors

    return all_vectors(vectors, texts)


def cmd_watch(args: argparse.Namespace) -> int:
    """Stream task strings through the frozen clustering and serve a live view."""
    from experiments.clustering import watch

    encoder = embeddings.get(args.embedding)
    if not embeddings.is_semantic(encoder):
        print("the null backend has nothing to watch; pick a real encoder")
        return 1
    source = args.source
    if source is None and not sys.stdin.isatty():
        source = "-"
    return watch.run(
        source,
        args.port,
        encoder=encoder,
        radius=args.radius,
        colour_weight=args.colour_weight,
        mask_verbs=args.mask_verbs,
        rule=args.rule,
        max_clusters=args.max_clusters,
    )


def cmd_separability(args: argparse.Namespace) -> int:
    """Premise check: are same-pairs and different-pairs separable at all?"""
    from experiments.clustering.evaluation import separability as sep

    encoder = embeddings.get(args.embedding)
    check = embeddings.verify(encoder)
    if not check.ok:
        print(f"refusing to report a result for {encoder.name}: verification failed")
        for failure in check.failures:
            print(f"  - {failure}")
        return 1

    pairs = _gold_for(args.set_name)
    texts = sorted(_vocab_for(args.set_name))
    vectors = embeddings.encode_all(encoder, texts)
    by_text = sep.all_vectors(vectors, texts)
    report = sep.separability(by_text, pairs)
    split = _split_for(args.set_name)
    probes = sep.probes(by_text, split.dev, split.heldout, split.dev_pair_groups)

    payload = {
        "experiment": "separability",
        "embedding": embeddings.describe(encoder),
        "gold_set": args.set_name,
        "verification": check.as_dict(),
        "split": split.report(),
        "views": {name: value.as_dict() for name, value in report.items()},
        "probes": {name: value.as_dict() for name, value in probes.items()},
    }
    suffix = "" if args.set_name == "hand" else f"-{args.set_name}"
    path = _write(f"separability-{encoder.name}{suffix}", payload)

    kind = "contextual" if embeddings.is_contextual(encoder) else "static"
    print(
        f"embedding {encoder.name} ({kind}) dim={encoder.dim}  strings={len(texts)}  "
        f"gold={args.set_name}"
    )
    print(f"verified: {'ok' if check.ok else 'FAILED'} {list(check.checks)}")
    print(
        f"{'view':7s} {'pos n':>5s} {'neg n':>5s} {'pos_min':>8s} {'pos_mean':>9s} "
        f"{'neg_max':>8s} {'neg_mean':>9s} {'thresh':>7s} {'bacc':>6s} {'merge':>6s} {'split':>6s}"
    )
    for value in report.values():
        print(
            f"{value.view:7s} {value.positives:5d} {value.negatives:5d} "
            f"{value.positive_min:8.3f} {value.positive_mean:9.3f} "
            f"{value.negative_max:8.3f} {value.negative_mean:9.3f} "
            f"{value.best_threshold:7.3f} {value.best_accuracy:6.3f} "
            f"{value.false_merges:6d} {value.false_splits:6d}"
        )
    for value in report.values():
        probe = probes[value.view]
        verdict = (
            f"clean below {value.clean_threshold:.3f}"
            if value.clean_threshold is not None
            else "classes interleave: no threshold separates them"
        )
        print(f"  {value.view}: {verdict}")
        print(
            f"    linear probe, fit on {probe.dev_pairs} dev pairs and scored on "
            f"{probe.heldout_pairs} held-out: {probe.heldout_balanced_accuracy:.3f} "
            f"(dev component-CV {probe.dev_cv_balanced_accuracy:.3f}, "
            f"dev train {probe.dev_balanced_accuracy:.3f}, alpha {probe.alpha:g})"
        )
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


def cmd_scale(args: argparse.Namespace) -> int:
    """Run the frozen configuration over a thousand strings and report what breaks."""
    from experiments.clustering import corpus, figures, scale

    config = scale.Config(
        encoder=args.embedding,
        radius=args.radius,
        colour_weight=args.colour_weight,
        mask_verbs=args.mask_verbs,
        rule=args.rule,
    )
    encoder = embeddings.get(config.encoder)
    if not embeddings.is_semantic(encoder):
        print("the null backend has nothing to scale; pick a real encoder")
        return 1

    synthetic = corpus.synthetic_corpus(args.target)
    real = corpus.real_strings(use_hub=not args.no_real, refresh=args.refresh_real)
    real = tuple(item for item in real if item.text not in {s.text for s in synthetic})
    print(f"corpus: {len(synthetic)} synthetic + {len(real)} real Hub strings")

    gold_synthetic = [item.label for item in synthetic]
    shuffled_items = corpus.shuffled(synthetic)
    matrices: dict[bool, np.ndarray] = {}
    encode_seconds = 0.0
    for mask in (True, False):
        matrices[mask], seconds = scale.encode(
            shuffled_items, encoder, replace(config, mask_verbs=mask)
        )
        if mask == config.mask_verbs:
            encode_seconds = seconds
    vectors = matrices[config.mask_verbs]

    headline = scale.stream(
        shuffled_items,
        vectors,
        config,
        name="synthetic",
        order="shuffled",
        confirm_top=args.confirm_top,
    )
    curve = scale.scale_curve(shuffled_items, vectors, config)
    sweep_runs = scale.sweep(synthetic, matrices, config)
    batches = scale.batch_sensitivity(synthetic, encoder, config)
    geometry = scale.geometry_report(shuffled_items, vectors, config.radius)

    control_items = corpus.minimal_corpus(args.control)
    control_vectors, _ = scale.encode(control_items, encoder, config)
    core = scale.core_run(synthetic, encoder, config)
    control = scale.stream(
        corpus.shuffled(control_items),
        control_vectors,
        config,
        name="control",
        order="shuffled",
        confirm_top=args.confirm_top,
    )

    rule_runs = scale.rule_sweep(synthetic, vectors, config)
    order = scale.order_sensitivity(shuffled_items, vectors, config)

    real_vectors, real_encode_seconds = (vectors[:0], 0.0)
    real_run: dict[str, object] = {}
    mixed_run: dict[str, object] = {}
    mixed_quality: dict[str, float] = {}
    if real:
        real_vectors, real_encode_seconds = scale.encode(real, encoder, config)
        real_run = scale.summary(
            scale.stream(
                corpus.shuffled(real),
                real_vectors,
                config,
                name="real",
                order="shuffled",
                confirm_top=args.confirm_top,
            )
        )
        mixed_items = list(shuffled_items) + list(corpus.shuffled(real))
        mixed_matrix = np.concatenate([vectors, real_vectors], axis=0)
        mixed = scale.stream(
            mixed_items,
            mixed_matrix,
            config,
            name="mixed",
            order="shuffled",
            confirm_top=args.confirm_top,
        )
        mixed_run = scale.summary(mixed)
        mixed_quality = scale.score_subset(mixed, mixed_items, "synthetic")
        mixed_run["synthetic_b_cubed"] = round(mixed_quality.get("b_cubed", 0.0), 4)
        mixed_run["synthetic_clusters"] = mixed_run["clusters"]

    payload: dict[str, Any] = {
        "config": config.describe(),
        "corpus": {
            "synthetic": len(synthetic),
            "real": len(real),
            "labels": len(set(gold_synthetic)),
            "coverage": scale.coverage_note(synthetic),
            "balance": corpus.balance(synthetic),
        },
        "cost": {
            "encode_seconds_synthetic": round(encode_seconds, 2),
            "encode_seconds_real": round(real_encode_seconds, 2),
            "strings_per_second": round(
                len(synthetic) / encode_seconds if encode_seconds else 0.0, 1
            ),
            "cluster_seconds": round(headline.cluster_seconds, 3),
        },
        "synthetic": scale.summary(headline, gold_synthetic),
        "scale_curve": [scale.summary(run, gold_synthetic[: run.n]) for run in curve],
        "sweep": [scale.summary(run, gold_synthetic) for run in sweep_runs],
        "rule_sweep": [scale.summary(run, gold_synthetic) for run in rule_runs],
        "diagnosis": {
            attribute: scale.fragmentation_by_attribute(headline, shuffled_items, attribute)
            for attribute in scale.ATTRIBUTES
        },
        "subsets": {
            f"{attribute}={value}": scale.subset_quality(headline, shuffled_items, attribute, value)
            for attribute in scale.ATTRIBUTES
            for value in ("none", "plastic", "red", "on the table", "pick up")
        },
        "order_sensitivity": order,
        "batch_sensitivity": batches,
        "geometry": geometry,
        "axes": scale.axis_report(shuffled_items, vectors),
        "axis_isolation": scale.axis_isolation(encoder, config),
        "control": scale.summary(control, [item.label for item in control_items]),
        "core": scale.summary(core, [item.label for item in synthetic]),
        "core_coverage": round(
            sum(1 for item in corpus.core_corpus(synthetic) if item.text == item.label)
            / len(synthetic),
            4,
        ),
        "real": real_run,
        "mixed": mixed_run,
    }

    path = _write("scale", payload)
    _print_scale_report(payload)
    print(f"\nwrote {path}")

    if args.figures:
        figures_for = (
            (mixed_items, mixed_matrix, mixed) if real else (shuffled_items, vectors, headline)
        )
        names = figures_for[2].arrival_labels
        base = Path("experiments/clustering/results")
        base.mkdir(parents=True, exist_ok=True)
        sizes: dict[int, int] = {}
        for label in names:
            sizes[label] = sizes.get(label, 0) + 1
        ordered = sorted(sizes)
        row_labels = [f"c{index}" for index in ordered]
        tags = {item.text: [f"colour:{attributes.colour_of(item.text)}"] for item in figures_for[0]}
        colours = sorted({tag for values in tags.values() for tag in values})
        members: dict[int, list[str]] = {}
        for label, item in zip(names, figures_for[0], strict=True):
            members.setdefault(label, []).append(item.text)
        matrix = figures.cluster_attribute_matrix(members, tags, colours)
        title = (
            f"{config.encoder} - {len(figures_for[0])} strings, "
            f"{len(ordered)} clusters, r={config.radius}"
        )
        for name, svg in (
            (
                "scale-treemap",
                figures.render_treemap([sizes[i] for i in ordered], row_labels, title),
            ),
            ("scale-map", figures.render_map(figures_for[1], names, title)),
            ("scale-heatmap", figures.render_heatmap(matrix, row_labels, colours, title)),
        ):
            (base / f"{name}.svg").write_text(svg, encoding="utf-8")
            print(f"wrote {base / f'{name}.svg'}")
    return 0


def _print_scale_report(payload: dict[str, Any]) -> None:
    """Tables straight from the payload, so the prose and the JSON cannot disagree."""

    def row(label: str, values: dict[str, object]) -> str:
        cells = " | ".join(f"{key}={value}" for key, value in values.items())
        return f"| {label} | {cells} |"

    print("\n## scale curve (synthetic, shuffled arrival order)")
    print("| n | clusters | singletons | largest | b_cubed | fragmentation | impure |")
    print("|---|---|---|---|---|---|---|")
    for entry in payload["scale_curve"]:
        print(
            row(
                str(entry["strings"]),
                {
                    "clusters": entry["clusters"],
                    "singletons": entry["singletons"],
                    "largest": entry["largest_cluster"],
                    "b_cubed": entry.get("b_cubed"),
                    "frag": entry.get("fragmentation"),
                    "impure": entry.get("impure_clusters"),
                },
            )
        )

    print("\n## radius and verb masking sweep (full synthetic corpus)")
    print("| run | clusters | singletons | largest | b_cubed | fragmentation | impure |")
    print("|---|---|---|---|---|---|---|")
    for entry in payload["sweep"]:
        print(
            row(
                str(entry["name"]),
                {
                    "clusters": entry["clusters"],
                    "singletons": entry["singletons"],
                    "largest": entry["largest_cluster"],
                    "b_cubed": entry.get("b_cubed"),
                    "frag": entry.get("fragmentation"),
                    "impure": entry.get("impure_clusters"),
                },
            )
        )

    print("\n## centroid rule at scale")
    print("| run | clusters | singletons | largest | b_cubed | fragmentation | impure |")
    print("|---|---|---|---|---|---|---|")
    for entry in payload["rule_sweep"]:
        print(
            row(
                str(entry["name"]),
                {
                    "clusters": entry["clusters"],
                    "singletons": entry["singletons"],
                    "largest": entry["largest_cluster"],
                    "b_cubed": entry.get("b_cubed"),
                    "frag": entry.get("fragmentation"),
                    "impure": entry.get("impure_clusters"),
                },
            )
        )

    print("\n## where it fragments, by attribute")
    print("| attribute=value | strings | clusters | modal share |")
    print("|---|---|---|---|")
    for attribute, buckets in payload["diagnosis"].items():
        for value, stats in buckets.items():
            print(row(f"{attribute}={value}", stats))

    print("\n## quality by subset")
    print("| subset | strings | b_cubed | fragmentation | impure |")
    print("|---|---|---|---|---|")
    for name, stats in payload["subsets"].items():
        if stats:
            print(row(name, stats))

    print("\n## object core (verb, colour, adjective and location stripped)")
    print(json.dumps(payload["core"], indent=2, sort_keys=True))
    print(f"core strings equal to the gold object: {payload['core_coverage']}")

    print("\n## control corpus (verb and colour variation only)")
    print(json.dumps(payload["control"], indent=2, sort_keys=True))

    print("\n## distance geometry at the shipped radius")
    print(json.dumps(payload["geometry"], indent=2, sort_keys=True))

    print("\n## separability per axis (everything else held identical)")
    print("| axis | same-object mean | different-object mean | margin | separable |")
    print("|---|---|---|---|---|")
    for axis, stats in payload["axes"].items():
        print(
            f"| {axis} | {stats['same_object_mean']} | {stats['different_object_mean']} "
            f"| {stats['margin']} | {stats['separable']} |"
        )

    print("\n## one axis at a time (48 objects, everything else fixed)")
    print("| axis | strings | same-object mean | different-object mean | margin | separable |")
    print("|---|---|---|---|---|---|")
    for axis, stats in payload["axis_isolation"].items():
        print(
            f"| {axis} | {int(stats['strings'])} | {stats['same_object_mean']} "
            f"| {stats['different_object_mean']} | {stats['margin']} "
            f"| {stats['separable']} |"
        )

    print("\n## headline and order")
    print(json.dumps(payload["synthetic"], indent=2, sort_keys=True))
    print(json.dumps(payload["order_sensitivity"], indent=2, sort_keys=True))
    print(json.dumps(payload["batch_sensitivity"], indent=2, sort_keys=True))
    print(json.dumps(payload["cost"], indent=2, sort_keys=True))
    if payload.get("real"):
        print("\n## real LeRobot sentences (no labels)")
        print(json.dumps(payload["real"], indent=2, sort_keys=True))
    if payload.get("mixed"):
        print("\n## mixed stream")
        print(json.dumps(payload["mixed"], indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.clustering")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch_parser = subparsers.add_parser(
        "watch", help="stream task strings and serve a live, confirmable clustering"
    )
    watch_parser.add_argument("--embedding", default="minilm")
    watch_parser.add_argument("--source", default=None, help="file of task strings, one per line")
    watch_parser.add_argument("--port", type=int, default=8765)
    watch_parser.add_argument("--rule", default="sliding_8")
    watch_parser.add_argument("--radius", type=float, default=0.15)
    watch_parser.add_argument("--colour-weight", type=float, default=1.0)
    watch_parser.add_argument("--mask-verbs", action="store_true")
    watch_parser.add_argument("--max-clusters", type=int, default=64)
    watch_parser.set_defaults(run=cmd_watch)

    scale_parser = subparsers.add_parser(
        "scale", help="run the frozen configuration over a thousand strings"
    )
    scale_parser.add_argument("--embedding", default="minilm")
    scale_parser.add_argument("--target", type=int, default=1200)
    scale_parser.add_argument("--rule", default="sliding_8")
    scale_parser.add_argument("--radius", type=float, default=0.15)
    scale_parser.add_argument("--colour-weight", type=float, default=1.0)
    scale_parser.add_argument("--mask-verbs", action="store_true")
    scale_parser.add_argument("--confirm-top", type=int, default=5)
    scale_parser.add_argument("--control", type=int, default=480)
    scale_parser.add_argument("--no-real", action="store_true")
    scale_parser.add_argument("--refresh-real", action="store_true")
    scale_parser.add_argument("--figures", action="store_true")
    scale_parser.set_defaults(run=cmd_scale)

    gold_parser = subparsers.add_parser("gold", help="report a gold set and its split")
    gold_parser.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    gold_parser.set_defaults(run=cmd_gold)

    centroids = subparsers.add_parser("centroids", help="decide the centroid rule on the dev split")
    centroids.add_argument("--embedding", default="m2v-8m")
    centroids.add_argument("--view", default="object", choices=("action", "object"))
    centroids.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    centroids.set_defaults(run=cmd_centroids)

    separate = subparsers.add_parser(
        "separability", help="check whether the two gold classes separate at all"
    )
    separate.add_argument("--embedding", default="m2v-8m")
    separate.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    separate.set_defaults(run=cmd_separability)

    project = subparsers.add_parser(
        "projection", help="fit the learned per-view projection and score it"
    )
    project.add_argument("--embedding", default="minilm")
    project.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    project.add_argument("--shrinkage", type=float, default=None)
    project.add_argument("--metric", default="euclidean", choices=sep_metrics())
    project.set_defaults(run=cmd_projection)

    final = subparsers.add_parser(
        "final", help="score the frozen configuration on held-out pairs and audit its edges"
    )
    final.add_argument("--embedding", default="minilm")
    final.add_argument("--view", default="object", choices=("action", "object"))
    final.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    final.add_argument("--rule", default="sliding_8")
    final.add_argument("--radius", type=float, default=0.30)
    final.add_argument("--colour-weight", type=float, default=0.0)
    final.add_argument("--mask-verbs", action="store_true")
    final.set_defaults(run=cmd_final)

    colours = subparsers.add_parser(
        "colours", help="choose the colour-facet weight on dev, sweeping the radius"
    )
    colours.add_argument("--embedding", default="minilm")
    colours.add_argument("--view", default="object", choices=("action", "object"))
    colours.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    colours.add_argument("--rule", default="sliding_8")
    colours.add_argument("--mask-verbs", action="store_true")
    colours.set_defaults(run=cmd_colours)

    plot = subparsers.add_parser(
        "plot", help="render the frozen clustering as treemap, PCA map, and heatmap"
    )
    plot.add_argument("--embedding", default="minilm")
    plot.add_argument("--view", default="object", choices=("action", "object"))
    plot.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    plot.add_argument("--rule", default="sliding_8")
    plot.add_argument("--radius", type=float, default=0.30)
    plot.add_argument("--colour-weight", type=float, default=0.0)
    plot.add_argument("--mask-verbs", action="store_true")
    plot.set_defaults(run=cmd_plot)

    args = parser.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
