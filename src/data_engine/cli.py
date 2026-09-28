"""CLI entry point. Functional commands are implemented in phase 05."""

from __future__ import annotations

import argparse


def main() -> None:
    """Parse CLI arguments; phase 05 implements API/worker startup."""
    parser = argparse.ArgumentParser(prog="de", description="Robot episode data engine")
    parser.add_argument("command", choices=["api", "worker", "dev", "doctor", "gc"])
    parser.parse_args()
    parser.error("commands are not implemented yet; see agents/implementation/status.md")
