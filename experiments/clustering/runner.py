"""Entry point for the stage 2.5 clustering experiments.

Run with `just cluster <subcommand>`. Subcommands are added as each experiment
lands, and each writes a JSON result under `experiments/clustering/results/` so a
result is reviewable and diffable rather than scrolled past in a terminal.

Order matters and is enforced by the runner: `factorial` and `audit` refuse to
run until the two upstream decisions they depend on - the centroid rule and the
view projection - have been frozen and recorded. A full factorial evaluated
before the centroid question is settled would multiply the search space by a
factor nobody needs, and the results would be impossible to read.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
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


#: The two gold sets. `hand` is realistic and small; `constructed` has labels
#: that are exact by construction and is an upper bound. Both are available to
#: every scoring command, because a conclusion is only worth stating when the two
#: agree - see `evaluation/gold_grid.py` for what each one can and cannot answer.
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


#: Candidate radii, in cosine distance. The grid brackets where the object view's
#: best similarity threshold sits (similarity 0.83-0.87, so distance 0.13-0.17) with
#: room either side, because the online algorithm needs a radius that produces a
#: usable number of clusters, not one that maximises a pairwise score.
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
    """Decide the centroid rule on dev data, before the factorial.

    Dev only, always. The held-out set exists to be touched once, and an
    exploratory sweep over nine rules on it would spend that for nothing.

    The radius is *selected here too*, on dev, rather than supplied. The previous
    version of this command took a radius as an argument and every reported number
    inherited whatever the caller typed - which is why the earlier run is marked
    invalid in the findings record. Over-merges are ranked first because a false
    merge silently mixes two tasks into one build, while a false split shows up as
    two small clusters a human fixes in seconds.
    """
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
    """Does the learned projection make the action view work by threshold alone?

    Reports the width sweep on dev, then the chosen width's score on held-out -
    which is read once, after everything is frozen.
    """
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
        # `None` means "use the module's measured default"; a literal 0.1 here
        # would silently override it and quietly reproduce the worst setting.
        shrinkage = args.shrinkage if args.shrinkage is not None else projection.DEFAULT_SHRINKAGE
        fitted, sweep = projection.choose_k(
            by_text, split.dev, view, encoder.name, shrinkage, metric=args.metric
        )
        projected = fitted.project(by_text)
        probe_before = sep.probes(by_text, split.dev, split.heldout, split.dev_pair_groups)[view]
        probe_after = sep.probes(projected, split.dev, split.heldout, split.dev_pair_groups)[view]

        metrics: dict[str, Any] = {}
        for metric in ("cosine", "euclidean"):
            # The dev "before" is the control that decides whether the projection
            # failed or merely overfitted. Without it a held-out drop is ambiguous.
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


#: A string whose distance to the centroid it joined falls within this multiple of
#: the radius counts as a boundary case. Comfortably inside means the assignment was
#: obvious; comfortably outside means the string sat alone.
BOUNDARY_MARGIN = 1.5


#: Candidate colour-facet weights. Zero is the current behaviour and is included so
#: the sweep can report what the facet buys rather than only that it was tried.
COLOUR_WEIGHTS = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0)


def cmd_colours(args: argparse.Namespace) -> int:
    """Choose the colour-facet weight on dev, sweeping the radius with it.

    The two are coupled - adding a facet changes the scale of every cosine distance,
    so a radius tuned without one is wrong for it - and they are therefore swept
    together rather than in sequence. Selection is on dev pairs only.
    """
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
        by_text = attributes.encode_with_colours(encoder, texts, weight)
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


def cmd_final(args: argparse.Namespace) -> int:
    """Score the frozen configuration on held-out pairs, once, and audit its edges.

    Everything here is fixed before this runs: encoder, view, centroid rule, radius.
    The clustering is fitted on held-out *strings* and scored against held-out
    *pairs*, so nothing about the answer was tuned on what is being graded.

    The audit exists because pair F1 alone cannot tell a human whether the clusters
    are right. A false merge at 0.941 looks identical to a correct merge. So the
    strings sitting near the decision boundary are dumped with the members they were
    merged with and the labels they were actually judged against, which is the only
    form in which "works well" is checkable.
    """
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
    by_text = attributes.encode_with_colours(encoder, vocabulary, args.colour_weight)

    heldout_texts = sorted({t for pair in split.heldout for t in (pair.a, pair.b)})
    heldout_vectors = np.stack([by_text[text] for text in heldout_texts])

    model = OnlineCentroids(radius=args.radius, rule_factory=centroid_experiment.RULES[args.rule])
    result = model.fit(heldout_vectors, heldout_texts)
    scores = ev.pair_scorer(split.heldout, args.view)(result.labels, heldout_texts, args.rule)

    # Every gold label this string was judged by, so the audit can say what the
    # string "actually" is rather than only what it was clustered with.
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
        # A true cosine distance, normalised on both sides. The clustering's own
        # `1 - dot` assumes unit points, and a centroid is a mean rather than a
        # point, so reporting that expression here would print a number the radius
        # is not actually compared against.
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
            # The failing pairs themselves, not just their count: an over-merge rate
            # of 0.286 does not say *which* pairs, and the whole point of this
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


def cmd_separability(args: argparse.Namespace) -> int:
    """Premise check: are same-pairs and different-pairs separable at all?

    If they interleave at every threshold then no radius exists that works, and
    every downstream number would be measuring an unachievable target. Worth
    knowing before spending a factorial on it.
    """
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.clustering")
    subparsers = parser.add_subparsers(dest="command", required=True)
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
    final.set_defaults(run=cmd_final)

    colours = subparsers.add_parser(
        "colours", help="choose the colour-facet weight on dev, sweeping the radius"
    )
    colours.add_argument("--embedding", default="minilm")
    colours.add_argument("--view", default="object", choices=("action", "object"))
    colours.add_argument("--set", dest="set_name", default="hand", choices=GOLD_SETS)
    colours.add_argument("--rule", default="sliding_8")
    colours.set_defaults(run=cmd_colours)

    args = parser.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
