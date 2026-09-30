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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.clustering")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gold", help="report the gold set and its split").set_defaults(
        run=cmd_gold
    )
    args = parser.parse_args(argv)
    result: int = args.run(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
