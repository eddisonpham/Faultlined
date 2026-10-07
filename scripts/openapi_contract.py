#!/usr/bin/env python3
"""Export the OpenAPI contract and detect drift against the committed copy (ADR 0009)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "docs" / "api" / "openapi.json"
sys.path.insert(0, str(ROOT / "src"))


def build() -> dict[str, Any]:
    from data_engine.api.app import create_app
    from data_engine.config import Settings

    app = create_app(Settings(_env_file=None), initialize_database=False)
    return app.openapi()


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate the committed contract")
    parser.add_argument(
        "--check", action="store_true", help="verify against the committed contract (default)"
    )
    args = parser.parse_args()

    current = render(build())

    if args.write:
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(current, encoding="utf-8")
        print(f"Wrote {CONTRACT.relative_to(ROOT)}")
        return 0

    if not CONTRACT.exists():
        print(
            f"{CONTRACT.relative_to(ROOT)} does not exist. "
            "Generate it deliberately with: python scripts/openapi_contract.py --write",
            file=sys.stderr,
        )
        return 1

    committed = CONTRACT.read_text(encoding="utf-8")
    if committed == current:
        print("OpenAPI contract matches the committed copy.")
        return 0

    old = json.loads(committed)
    new = json.loads(current)
    changed: list[str] = []
    for section in ("paths", "components"):
        before_keys = set(old.get(section, {}))
        after_keys = set(new.get(section, {}))
        for key in sorted(after_keys - before_keys):
            changed.append(f"  added   {section}.{key}")
        for key in sorted(before_keys - after_keys):
            changed.append(f"  removed {section}.{key}")
        for key in sorted(before_keys & after_keys):
            if old[section][key] != new[section][key]:
                changed.append(f"  changed {section}.{key}")

    print("OpenAPI contract has drifted from the committed copy:", file=sys.stderr)
    print("\n".join(changed) or "  (structure identical; formatting differs)", file=sys.stderr)
    print(
        "\nIf the change is intended, refresh the contract with:\n"
        "  python scripts/openapi_contract.py --write",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
