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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from experiments.clustering import embeddings
from experiments.clustering.evaluation import gold, splits

RESULTS = Path(__file__).parent / "results"


def _write(name: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def cmd_gold(_args: argparse.Namespace) -> int:
    """Report the gold set and the leakage report for the dev/held-out split."""
    result = splits.split()
    payload = {
        "experiment": "gold",
        "summary": gold.summary(),
        "split": result.report(),
        "ambiguous_pairs": [
            {"a": pair.a, "b": pair.b, "note": pair.note}
            for pair in gold.GOLD_PAIRS
            if pair.ambiguous
        ],
    }
    path = _write("gold", payload)
    print(json.dumps({k: payload[k] for k in ("summary", "split")}, indent=2))
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    if not result.clean:
        print("\nFAILED: the split leaks; no result from this harness is trustworthy")
        return 1
    return 0


def cmd_centroids(args: argparse.Namespace) -> int:
    """Decide the centroid rule on dev data, before the factorial.

    Dev only, always. The held-out set exists to be touched once, and an
    exploratory sweep over nine rules on it would spend that for nothing.
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

    split = splits.split()
    texts = sorted({t for pair in split.dev for t in (pair.a, pair.b)})
    scorer = ev.pair_scorer(split.dev, "action")

    outcomes = []
    for rule in centroid_experiment.RULES:
        outcome = centroid_experiment.run_rule(
            rule,
            encoder,
            texts,
            radius=args.radius,
            scorer=scorer,
        )
        outcomes.append(outcome)

    ranked = sorted(outcomes, key=lambda o: (o.drift, o.outlier_pull))
    payload = {
        "experiment": "centroids",
        "embedding": embeddings.describe(encoder),
        "radius": args.radius,
        "split": "dev",
        "inputs": len(texts),
        "outcomes": [o.as_dict() for o in ranked],
    }
    path = _write(f"centroids-{encoder.name}", payload)

    print(f"embedding {encoder.name} dim={encoder.dim}  inputs={len(texts)}  radius={args.radius}")
    print(
        f"{'rule':22s} {'drift':>7s} {'pull':>7s} {'recov':>7s} "
        f"{'us/pt':>7s} {'mem':>4s} {'k':>3s} {'kvar':>5s} {'pairF1':>7s} {'over':>6s}"
    )
    for o in ranked:
        print(
            f"{o.rule:22s} {o.drift:7.4f} {o.outlier_pull:7.4f} {o.recovery:7.3f} "
            f"{o.micros_per_point:7.1f} {o.points_retained:4d} {o.cluster_count:3d} "
            f"{o.distinct_counts:5d} {o.pair_f1:7.3f} {o.over_merge_rate:6.3f}"
        )
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


def cmd_separability(args: argparse.Namespace) -> int:
    """Premise check: are same-pairs and different-pairs separable at all?

    If they interleave at every threshold then no radius exists that works, and
    every downstream number would be measuring an unachievable target. Worth
    knowing before spending a factorial on it.
    """
    from experiments.clustering.evaluation import separability as sep

    encoder = embeddings.get(args.embedding)
    texts = sorted(gold.vocab())
    vectors = embeddings.encode_all(encoder, texts)
    by_text = sep.all_vectors(vectors, texts)
    report = sep.separability(by_text)

    payload = {
        "experiment": "separability",
        "embedding": embeddings.describe(encoder),
        "views": {name: value.as_dict() for name, value in report.items()},
    }
    path = _write(f"separability-{encoder.name}", payload)

    print(f"embedding {encoder.name} dim={encoder.dim}  strings={len(texts)}")
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
        verdict = (
            f"clean below {value.clean_threshold:.3f}"
            if value.clean_threshold is not None
            else "classes interleave: no threshold separates them"
        )
        print(f"  {value.view}: {verdict}")
    print(f"\nwrote {path.relative_to(Path.cwd())}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.clustering")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gold", help="report the gold set and its split").set_defaults(
        run=cmd_gold
    )
    centroids = subparsers.add_parser("centroids", help="decide the centroid rule on the dev split")
    centroids.add_argument("--embedding", default="m2v-8m")
    centroids.add_argument("--radius", type=float, default=0.35)
    centroids.set_defaults(run=cmd_centroids)

    separate = subparsers.add_parser(
        "separability", help="check whether the two gold classes separate at all"
    )
    separate.add_argument("--embedding", default="m2v-8m")
    separate.set_defaults(run=cmd_separability)

    args = parser.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
