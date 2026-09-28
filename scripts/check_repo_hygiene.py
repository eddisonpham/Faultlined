#!/usr/bin/env python3
"""Repository hygiene: required structure, relative Markdown links, ADR numbering, secret patterns."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = [
    "CLAUDE.md",
    "README.md",
    ".env.example",
    "agents/README.md",
    "agents/HANDOFF.md",
    *(f"agents/{d}" for d in (
        "spec", "architecture", "research", "implementation", "testing",
        "benchmarking", "observability", "experiments", "reviews", "decisions", "roles", "prompts",
    )),
]

SECRET_PATTERNS = {
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "AWS access key ID": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".next", "htmlcov",
}
SKIP_FILES = {".env", ".env.local"}  # gitignored local secrets are never scanned or read
MAX_SCAN_BYTES = 2_000_000
LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
ADR_RE = re.compile(r"^(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
VERBATIM_SPECS = {"00-original-specification.md", "01-scaffolding-instructions.md"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir() or path.name in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def check_structure(root: Path) -> list[str]:
    return [f"missing required path: {p}" for p in REQUIRED if not (root / p).exists()]


def check_links(root: Path) -> list[str]:
    errors: list[str] = []
    docs = [p for p in iter_files(root) if p.suffix == ".md" and p.name not in VERBATIM_SPECS]
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in LINK_RE.findall(text):
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            rel = target.split("#", 1)[0]
            if rel and not (doc.parent / rel).resolve().exists():
                errors.append(f"broken link in {doc.relative_to(root)}: {target}")
    return errors


def check_adrs(root: Path) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    directory = root / "agents" / "decisions"
    if not directory.is_dir():
        return errors
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        match = ADR_RE.match(path.name)
        if not match:
            errors.append(f"ADR filename must be NNNN-kebab-slug.md: {path.name}")
            continue
        number = match.group(1)
        if number in seen:
            errors.append(f"duplicate ADR number {number}: {seen[number]} and {path.name}")
        seen[number] = path.name
    return errors


def check_secrets(root: Path) -> list[str]:
    errors: list[str] = []
    for path in iter_files(root):
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"possible {label} in {path.relative_to(root)}")
    return errors


def main(root: Path = ROOT) -> int:
    errors = [*check_structure(root), *check_links(root), *check_adrs(root), *check_secrets(root)]
    if errors:
        print("Repository hygiene check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Repository hygiene: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT))
