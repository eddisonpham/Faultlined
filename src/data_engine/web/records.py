"""Benchmarks and Experiments pages (stage 3, `frontend` criterion)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

__all__ = [
    "Experiment",
    "benchmarks_fragment",
    "benchmarks_model",
    "benchmarks_page",
    "experiments_fragment",
    "experiments_model",
    "experiments_page",
    "repo_root",
]

RESULT_LIMIT = 40

_META_RE = re.compile(r"^-\s+\*\*([^*]+):\*\*\s*(.+?)\s*$")
_TITLE_RE = re.compile(r"^#\s+(EXP-\d+[a-z]?):\s*(.+?)\s*$")
_RECORD_RE = re.compile(r"^(\d{4}[a-z]?)-[a-z0-9][a-z0-9-]*\.md$")


def repo_root() -> Path | None:
    """The checkout root, or None when the app runs from an installed wheel."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "benchmarks").is_dir() and (candidate / "agents").is_dir():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class Run:
    """One file from ``benchmarks/`` flattened to what the table renders."""

    name: str
    version: str
    label: str
    is_baseline: bool
    recorded: str
    p50: float | None
    mean: float | None
    trials: int | None
    failures: int | None
    throughput: float | None
    commit: str
    dirty: bool | None
    run_id: str
    source_sha256: str
    status: str


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any] | None:
    """One benchmark document, or None when it is missing or unparseable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return document if isinstance(document, dict) else None


def _run_from(document: dict[str, Any], path: Path, *, is_baseline: bool) -> Run:
    benchmark = document.get("benchmark") or {}
    config = document.get("config") or {}
    summary = document.get("summary") or {}
    provenance = document.get("provenance") or {}
    git_commit = str(provenance.get("git_commit") or "")
    return Run(
        name=str(benchmark.get("name") or path.stem),
        version=str(benchmark.get("version") or ""),
        label=path.stem,
        is_baseline=is_baseline,
        recorded=str(document.get("started_at") or provenance.get("timestamp_utc") or ""),
        p50=_number(summary.get("p50_seconds")),
        mean=_number(summary.get("mean_seconds")),
        trials=int(_number(summary.get("n")) or 0) or None,
        failures=int(_number(summary.get("failure_count")) or 0),
        throughput=_number(config.get("throughput_mib_per_second_p50")),
        commit=git_commit[:12],
        dirty=bool(provenance["git_dirty"]) if "git_dirty" in provenance else None,
        run_id=str(document.get("run_id") or document.get("source_run_id") or ""),
        source_sha256=str(config.get("source_sha256") or ""),
        status=str(document.get("status") or ("baseline" if is_baseline else "unknown")),
    )


def _newest_first(runs: list[Run]) -> list[Run]:
    return sorted(
        runs,
        key=lambda run: (run.recorded or "", run.run_id),
        reverse=True,
    )


def benchmarks_model() -> dict[str, Any]:
    """Committed baselines and recorded runs, or a reason the page is empty."""
    root = repo_root()
    if root is None:
        return {
            "baselines": [],
            "results": [],
            "unreadable": [],
            "error": "repository root not found",
        }

    baselines: list[Run] = []
    results: list[Run] = []
    unreadable: list[str] = []
    for path in sorted((root / "benchmarks" / "baselines").glob("*.json")):
        document = _load_json(path)
        if document is None:
            unreadable.append(path.name)
        else:
            baselines.append(_run_from(document, path, is_baseline=True))
    for path in sorted((root / "benchmarks" / "results").glob("*.json")):
        document = _load_json(path)
        if document is None:
            unreadable.append(f"results/{path.name}")
        else:
            results.append(_run_from(document, path, is_baseline=False))

    return {
        "baselines": baselines,
        "results": _newest_first(results)[:RESULT_LIMIT],
        "result_total": len(results),
        "unmatched": sorted({run.name for run in results} - {run.name for run in baselines}),
        "unreadable": unreadable,
        "error": "",
    }


@dataclass(frozen=True, slots=True)
class Experiment:
    """The four metadata fields every experiment record leads with."""

    exp_id: str
    title: str
    status: str
    date: str
    author: str
    href: str
    preview: str


def _first_heading_after(text: str, names: tuple[str, ...]) -> str:
    """The body of the first ``## <name>`` section, in characters, for a preview."""
    lowered = {name.lower() for name in names}
    for index, line in enumerate(text.splitlines()):
        if not line.startswith("## "):
            continue
        heading = line[3:].strip().lower()
        if any(heading.startswith(name) for name in lowered):
            body: list[str] = []
            for follow in text.splitlines()[index + 1 :]:
                if follow.startswith("## "):
                    break
                body.append(follow)
            return " ".join(part.strip() for part in body if part.strip())
    return ""


def experiment_preview(path: Path, limit: int = 220) -> str:
    """The record's own Result/Verdict prose, trimmed — not a summary we invent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    body = _first_heading_after(text, ("result", "verdict", "outcome", "conclusion", "finding"))
    if not body:
        return ""
    preview = body if len(body) <= limit else body[: limit - 1].rstrip() + "…"
    return preview.replace("/api/v1/", "/…/").replace("/api/v1", "")


def experiments_model() -> dict[str, Any]:
    """Every experiment record on disk, newest id first."""
    root = repo_root()
    if root is None:
        return {"experiments": [], "error": "repository root not found"}
    directory = root / "agents" / "experiments"
    records: list[Experiment] = []
    for path in sorted(directory.glob("*.md")):
        match = _RECORD_RE.match(path.name)
        if match is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        title = ""
        meta: dict[str, str] = {}
        for line in lines:
            title_match = _TITLE_RE.match(line)
            if title_match and not title:
                title = title_match.group(2)
                continue
            meta_match = _META_RE.match(line)
            if meta_match:
                meta[meta_match.group(1).strip().lower()] = meta_match.group(2)
        records.append(
            Experiment(
                exp_id=f"EXP-{match.group(1)}",
                title=title or path.stem,
                status=meta.get("status", "unrecorded"),
                date=meta.get("date (utc)", meta.get("date", "")),
                author=meta.get("author/agent", meta.get("author", "")),
                href=f"agents/experiments/{path.name}",
                preview=experiment_preview(path),
            )
        )
    return {
        "experiments": sorted(records, key=lambda record: record.exp_id, reverse=True),
        "error": "",
    }


def _seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1:
        return f"{value:.3f} s"
    return f"{value * 1000:.3f} ms"


def _throughput(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} MiB/s"


def _when(value: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return escape(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return escape(parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M"))


def _commit(run: Run) -> str:
    if not run.commit:
        return "—"
    return escape(run.commit) + ("*" if run.dirty else "")


def _status_badge(status: str) -> str:
    """`ok` reads as success and anything else as a warning, using terminal-ui's own text-colour"""

    css = {"ok": "success", "baseline": "info", "pass": "success"}.get(status, "warning")
    return f'<span class="text-{css}">{escape(status)}</span>'


def _readouts(pairs: list[tuple[str, str, str]]) -> str:
    from data_engine.web.pages import _readouts as render

    return render(pairs)


def benchmarks_page(model: dict[str, Any], theme: str) -> str:
    from data_engine.web.pages import _page

    return _page("Benchmarks", "/ui/benchmarks", _benchmarks_body(model), theme)


def benchmarks_fragment(model: dict[str, Any]) -> str:
    return _benchmarks_body(model)


def _benchmarks_body(model: dict[str, Any]) -> str:
    from data_engine.web.pages import _empty, _section, _table

    if model.get("error"):
        return _empty("benchmark records unavailable", str(model["error"]))
    baselines: list[Run] = model.get("baselines") or []
    results: list[Run] = model.get("results") or []
    if not baselines and not results:
        return _empty(
            "no benchmark records on disk",
            "run just bench to write one",
        )

    readouts = [
        ("baselines", str(len(baselines)), "committed claims"),
        ("runs", str(model.get("result_total", 0)), "records in benchmarks/results"),
        (
            "measured",
            _throughput(baselines[0].throughput if baselines else None),
            "slowest baseline P50",
        ),
    ]
    parts = [_readouts(readouts)]

    unmatched: list[str] = model.get("unmatched") or []
    if baselines:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(run.name)}</code></td>"
            f'<td class="num">{_seconds(run.p50)}</td>'
            f'<td class="num">{_throughput(run.throughput)}</td>'
            f'<td class="num">{escape(str(run.trials or "—"))}</td>'
            f'<td class="num">{escape(str(run.failures))}</td>'
            f"<td>{_commit(run)}</td>"
            f"<td>{escape(run.source_sha256[:12]) if run.source_sha256 else '—'}</td>"
            "</tr>"
            for run in baselines
        )
        parts.append(
            _section(
                "Committed baselines",
                _table(
                    "Committed benchmark baselines",
                    "<th>workload</th><th class='num'>P50</th><th class='num'>throughput</th>"
                    "<th class='num'>trials</th><th class='num'>failures</th><th>commit</th>"
                    "<th>input sha256</th>",
                    rows,
                )
                + '<p class="de-dim">A baseline is a claim about one machine and one input. '
                "The input hash is what lets a later run prove it measured the same bytes; a "
                "different hash is a different measurement, not a regression."
                + (
                    " No baseline is committed for: "
                    + ", ".join(f"<code>{escape(name)}</code>" for name in unmatched)
                    + "."
                    if unmatched
                    else ""
                )
                + "</p>",
                "reference",
            )
        )

    if results:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(run.name)}</code></td>"
            f"<td>{_status_badge(run.status)}</td>"
            f'<td class="num">{_seconds(run.p50)}</td>'
            f'<td class="num">{_throughput(run.throughput)}</td>'
            f'<td class="num">{escape(str(run.trials or "—"))}</td>'
            f"<td>{_when(run.recorded)}</td>"
            f"<td>{_commit(run)}</td>"
            "</tr>"
            for run in results
        )
        parts.append(
            _section(
                "Recent runs",
                _table(
                    "Recorded benchmark runs, newest first",
                    "<th>workload</th><th>status</th><th class='num'>P50</th>"
                    "<th class='num'>throughput</th><th class='num'>trials</th>"
                    "<th>when</th><th>commit</th>",
                    rows,
                )
                + f'<p class="de-dim">Newest {RESULT_LIMIT} of {model.get("result_total", 0)} '
                "records in <code>benchmarks/results/</code>, which is gitignored: these are "
                "observations from this machine, not claims. The committed table above is the "
                "claim.</p>",
                "observed",
            )
        )

    unreadable: list[str] = model.get("unreadable") or []
    if unreadable:
        parts.append(
            _section(
                "Unreadable records",
                _empty(
                    f"{len(unreadable)} file(s) could not be parsed",
                    "a truncated record from an interrupted run is skipped, not fatal",
                )
                + "<p class='de-dim'>"
                + escape(", ".join(unreadable[:10]))
                + "</p>",
            )
        )
    return "".join(parts)


def experiments_page(model: dict[str, Any], theme: str) -> str:
    from data_engine.web.pages import _page

    return _page("Experiments", "/ui/experiments", _experiments_body(model), theme)


def experiments_fragment(model: dict[str, Any]) -> str:
    return _experiments_body(model)


def _experiments_body(model: dict[str, Any]) -> str:
    from data_engine.web.pages import _empty, _section, _table

    if model.get("error"):
        return _empty("experiment records unavailable", str(model["error"]))
    records: list[Experiment] = model.get("experiments") or []
    if not records:
        return _empty("no experiment records", "records live in agents/experiments/")

    accepted = sum(1 for record in records if record.status.startswith("accepted"))
    parts = [
        _readouts(
            [
                ("records", str(len(records)), "EXP files in agents/experiments"),
                ("accepted", str(accepted), "status line reads accepted"),
                (
                    "latest",
                    records[0].exp_id,
                    escape(records[0].date.split("measured")[-1].strip() or records[0].date),
                ),
            ]
        )
    ]

    rows = []
    for record in records:
        rows.append(
            "<tr>"
            f"<td>{escape(record.exp_id)}</td>"
            f"<td>{escape(record.title)}</td>"
            f"<td>{escape(record.status)}</td>"
            f"<td>{escape(record.date)}</td>"
            f"<td>{escape(record.author)}</td>"
            f"<td class='de-dim'>{escape(record.preview)}</td>"
            f"<td><code>{escape(record.href)}</code></td>"
            "</tr>"
        )
    parts.append(
        _section(
            "Experiment records",
            _table(
                "Experiment records, newest first",
                "<th>id</th><th>what</th><th>status</th><th>date</th><th>author</th>"
                "<th>result</th><th>record</th>",
                "".join(rows),
            )
            + '<p class="de-dim">The Result column is the record\'s own prose, not a summary '
            "written here. Every number this project claims is traceable to a row on this "
            "page.</p>",
            "provenance",
        )
    )
    return "".join(parts)
