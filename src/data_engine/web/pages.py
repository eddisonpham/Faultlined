"""Server-rendered UI for the MVP Status / Jobs / Artifacts slice.

Per ADR 0014 this is server-rendered HTML with vanilla-JS polling: no framework,
no bundler, no build step, and no JavaScript dependency. The terminal/instrument
vocabulary comes from a vendored stylesheet (see vendor/terminal-ui/NOTICE.md);
this module owns layout, semantics, and escaping.

Polling re-requests the same page with ``X-Fragment: 1`` and swaps the returned
HTML. The alternative, re-rendering JSON in JavaScript, would mean writing every
row twice and two renderers eventually disagreeing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
VENDOR = HERE / "vendor" / "terminal-ui"
CORE_URL = "/ui/vendor/terminal-ui/core.css"
THEME_URL = "/ui/vendor/terminal-ui/theme-{name}.css"
LAYOUT_URL = "/ui/faultlined.css"

# Upstream ships more themes; these four are vendored. See vendor/terminal-ui/NOTICE.md.
THEMES = ("vt220", "amber", "github-dark", "monochrome")
DEFAULT_THEME = "vt220"

NAV_LINKS = (
    ("/ui", "Status"),
    ("/ui/jobs", "Jobs"),
    ("/ui/episodes", "Episodes"),
    ("/ui/insights", "Insights"),
    ("/ui/metrics", "Metrics"),
    ("/ui/artifacts", "Artifacts"),
    ("/docs", "API"),
)

FONT_URL = "/ui/vendor/departure-mono/DepartureMono-Regular.woff2"
FONT_PATH = HERE / "vendor" / "departure-mono" / "DepartureMono-Regular.woff2"

# Curation views on the Episodes page; mirrors catalog.list_episodes flags.
EPISODE_FLAGS = ("", "jerky", "stalled", "short", "long")
EPISODE_STATES = ("", "ingested", "valid", "quarantined")

JOB_STATES = (
    "queued",
    "running",
    "retrying",
    "cancel_requested",
    "succeeded",
    "failed",
    "canceled",
    "timed_out",
)
ACTIVE_STATES = ("queued", "running", "retrying", "cancel_requested")
# States a cancel request can still act on (ADR 0015: terminal jobs are immutable).
CANCELLABLE_STATES = ("queued", "running", "retrying")

# Progress a job is expected to pass through, for the detail strip.
STRIP_STEPS = ("queued", "running", "succeeded")


def vendor_css(name: str) -> str:
    return (VENDOR / name).read_text(encoding="utf-8")


def font_bytes() -> bytes:
    return FONT_PATH.read_bytes()


def layout_css() -> str:
    return (HERE / "faultlined.css").read_text(encoding="utf-8")


def _theme(name: str | None) -> str:
    return name if name in THEMES else DEFAULT_THEME


def _nav(active: str, theme: str) -> str:
    links = " ".join(
        f'<a href="{href}"{' aria-current="page"' if href == active else ""}>{label}</a>'
        for href, label in NAV_LINKS
    )
    options = "".join(
        f'<option value="{t}"{" selected" if t == theme else ""}>{t}</option>' for t in THEMES
    )
    return (
        '<nav class="de-nav">'
        '<span class="de-brand">Faultlined<span> / data engine</span></span>'
        f"{links}"
        '<span class="de-spacer"></span>'
        f'<form method="get" action="{active}"><select class="de-theme" name="theme" '
        f'onchange="this.form.submit()">{options}</select></form>'
        "</nav>"
    )


def _page(title: str, active: str, body: str, theme: str, script: str = "") -> str:
    return (
        '<!doctype html><html lang="en" data-theme="' + theme + '">'
        '<head><meta charset="utf-8">'
        f"<title>{escape(title)} // Faultlined</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<link rel="stylesheet" href="{CORE_URL}">'
        f'<link rel="stylesheet" href="{THEME_URL.format(name=theme)}">'
        f'<link rel="stylesheet" href="{LAYOUT_URL}">'
        f'</head><body><div class="de-shell">{_nav(active, theme)}'
        f'<div class="de-head"><h1>{escape(title)}</h1>'
        '<span class="de-clock" data-clock>--:--:--</span></div>'
        f"{body}"
        f"<script>{script}</script></div></body></html>"
    )


# ---------------------------------------------------------------- primitives


def _readouts(pairs: list[tuple[str, str, str]]) -> str:
    cells = "".join(
        f'<div class="de-readout"><dt>{escape(label)}</dt>'
        f"<dd>{value}<small>{escape(unit)}</small></dd></div>"
        for label, value, unit in pairs
    )
    return f'<dl class="de-readouts">{cells}</dl>'


def _meter(fraction: float, width: int = 24) -> str:
    """ASCII meter, e.g. [#########.............] 38%."""
    filled = max(0, min(width, round(fraction * width)))
    return (
        f'<span class="de-meter">[{"#" * filled}'
        f'<span class="off">{"." * (width - filled)}</span>] '
        f"{round(fraction * 100)}%</span>"
    )


def _verdict_badge(verdict: Any) -> str:
    css = {"smooth": "success", "moderate": "warning", "jerky": "error"}.get(
        str(verdict), "comment"
    )
    return f'<span class="text-{css}">{escape(str(verdict))}</span>'


def _sparkline(values: list[float], *, width: int = 260, height: int = 44) -> str:
    """SVG trace of a series on the graticule; one `<polyline>`, nothing fetched."""
    data = [float(v) for v in values] or [0.0]
    lo, hi = min(data), max(data)
    span = (hi - lo) or 1.0
    step = width / max(len(data) - 1, 1)
    points = " ".join(
        f"{i * step:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}"
        for i, v in enumerate(data)
    )
    return (
        f'<svg class="de-chart" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img">'
        f'<polyline points="{points}"/></svg>'
    )


def _histogram_svg(bins: list[dict[str, Any]], *, width: int = 260, height: int = 44) -> str:
    """Bar chart of [{count}] bins on the same graticule as the sparkline."""
    if not bins:
        return _sparkline([])
    peak = max((int(b.get("count") or 0) for b in bins), default=0) or 1
    bar = width / len(bins)

    def rect(index: int, count: int) -> str:
        bar_h = count / peak * (height - 6)
        return (
            f'<rect x="{index * bar:.1f}" y="{height - 3 - bar_h:.1f}" '
            f'width="{max(bar - 1, 1):.1f}" height="{bar_h:.1f}"/>'
        )

    rects = "".join(rect(i, int(b.get("count") or 0)) for i, b in enumerate(bins))
    return (
        f'<svg class="de-chart de-chart-bars" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img">{rects}</svg>'
    )


def _scatter_svg(
    items: list[dict[str, Any]], key: str, *, width: int = 260, height: int = 44
) -> str:
    """One dot per episode along the x axis, value on the y axis."""
    values = [float(i.get(key) or 0) for i in items] or [0.0]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / max(len(values) - 1, 1)

    def dot(index: int, value: float) -> str:
        cy = height - 3 - (value - lo) / span * (height - 6)
        return f'<circle cx="{index * step:.1f}" cy="{cy:.1f}" r="2"/>'

    dots = "".join(dot(i, v) for i, v in enumerate(values))
    return (
        f'<svg class="de-chart de-chart-dots" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img">{dots}</svg>'
    )


def _state_badge(state: str) -> str:
    css = {
        "succeeded": "success",
        "failed": "error",
        "timed_out": "error",
        "canceled": "comment",
        "cancel_requested": "warning",
        "running": "info",
        "retrying": "warning",
    }.get(state, "comment")
    return f'<span class="text-{css}">{escape(state)}</span>'


def _when(value: Any) -> str:
    if not value:
        return '<span class="text-comment">-</span>'
    return escape(str(value))[:19].replace("T", " ")


def _bytes(value: Any) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)[:4000]
    except TypeError, ValueError:
        return str(value)


_CLOCK_JS = """
(function () {
  var el = document.querySelector('[data-clock]');
  if (!el) return;
  var tick = function () {
    var d = new Date();
    el.textContent = d.toISOString().slice(11, 19) + ' UTC';
  };
  tick();
  setInterval(tick, 1000);
})();
"""

# Intervals follow agents/architecture/frontend.md: jobs list 3 s, detail 5 s,
# and polling pauses while the tab is hidden.
_POLL_JS = """
(function (url, ms) {
  setInterval(function () {
    if (document.hidden) return;
    fetch(url, {headers: {'X-Fragment': '1'}})
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (t) {
        if (t) document.querySelector('[data-poll]').innerHTML = t;
      })
      .catch(function () { /* keep the last good render on a transient error */ });
  }, ms);
})(%s, %d);
"""


def _script(url: str | None, ms: int) -> str:
    return _CLOCK_JS + ("" if url is None else _POLL_JS % (json.dumps(url), ms))


# Counts the remaining job budget down in the browser so a deadline does not need a
# poll to look alive. Purely presentational: the server still owns the timeout.
_DEADLINE_JS = """
(function () {
  var el = document.querySelector('[data-deadline]');
  if (!el) return;
  var deadline = Number(el.getAttribute('data-deadline')) * 1000;
  var tick = function () {
    var left = Math.round((deadline - Date.now()) / 1000);
    if (left <= 0) {
      el.textContent = Math.abs(Math.round(left / 60)) + 'm over';
      el.className = 'text-error';
      return;
    }
    el.textContent = Math.round(left / 60) + 'm left';
  };
  tick();
  setInterval(tick, 15000);
})();
"""


# ---------------------------------------------------------------- status


def status_page(model: dict[str, Any], theme: str) -> str:
    body = (
        f'<p class="de-sub">live telemetry // poll 3s</p><div data-poll>{_status_body(model)}</div>'
    )
    return _page("Status", "/ui", body, theme, _script("/ui", 3000))


def status_fragment(model: dict[str, Any]) -> str:
    return _status_body(model)


def _status_body(model: dict[str, Any]) -> str:
    res = model.get("resources", {})
    depth = model.get("queue_depth", {})
    artifacts = int(model.get("artifact_count", 0))
    episodes = int(model.get("episode_count", 0))
    total_jobs = sum(int(v) for v in depth.values())
    active = sum(int(depth.get(k, 0)) for k in ACTIVE_STATES)
    cpu = res.get("cpu_percent")
    mem = res.get("memory_used_bytes")
    disk = res.get("disk_free_bytes")
    gpu = res.get("gpu_model") or ("present" if res.get("gpu_present") else "absent")

    readouts = _readouts(
        [
            ("health", '<span class="text-success">ok</span>', ""),
            ("active jobs", str(active), f"/ {total_jobs}"),
            ("artifacts", str(artifacts), ""),
            ("episodes", str(episodes), ""),
            ("cpu", f"{cpu:.1f}" if cpu is not None else "n/a", "%"),
            ("memory", escape(_bytes(mem)), ""),
            ("disk free", escape(_bytes(disk)), ""),
            ("gpu", escape(str(gpu)), ""),
        ]
    )

    peak = max([int(v) for v in depth.values()] + [1])
    rows = "".join(
        f'<tr><td>{escape(str(k))}</td><td class="num">{v}</td>'
        f"<td>{_meter(int(v) / peak)}</td></tr>"
        for k, v in depth.items()
    )
    queue = (
        '<section class="de-section"><h2>Queue depth by state</h2>'
        '<table class="de-table"><thead><tr><th>State</th><th class="num">Jobs</th>'
        f"<th>Load</th></tr></thead><tbody>{rows}</tbody></table></section>"
    )

    return readouts + queue


# ---------------------------------------------------------------- jobs


def jobs_page(model: dict[str, Any], state_filter: str | None, theme: str) -> str:
    query = f"?state={escape(state_filter)}" if state_filter else ""
    body = (
        '<p class="de-sub">newest first // poll 3s</p>'
        f'<form class="de-filters" method="get" action="/ui/jobs">'
        '<label for="state">filter</label>'
        '<select id="state" name="state" onchange="this.form.submit()">'
        f"{_options(state_filter)}</select>"
        '<button type="submit">apply</button></form>'
        f"<div data-poll>{_jobs_body(model)}</div>"
    )
    return _page("Jobs", "/ui/jobs", body, theme, _script("/ui/jobs" + query, 3000))


def jobs_fragment(model: dict[str, Any]) -> str:
    return _jobs_body(model)


def _options(state_filter: str | None) -> str:
    return "".join(
        f'<option value="{s}"{" selected" if s == state_filter else ""}>{s or "all"}</option>'
        for s in ("", *JOB_STATES)
    )


def _attempts(job: dict[str, Any]) -> str:
    """`2/3` plus a meter, so retry pressure is visible in a list of 50 rows."""
    used = int(job.get("attempts") or 0)
    budget = max(int(job.get("max_attempts") or 1), 1)
    if used == 0:
        return '<span class="text-comment">-</span>'
    return f'<span class="de-attempts">{used}/{budget} {_meter(used / budget, 10)}</span>'


def _jobs_body(model: dict[str, Any]) -> str:
    items = model.get("items", [])
    if not items:
        return '<p class="de-empty">// no jobs recorded</p>'
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/jobs/{escape(str(i["id"]))}">{escape(str(i["id"])[:8])}</a></td>'
        f"<td>{escape(str(i['type']))}</td>"
        f"<td>{_state_badge(str(i['state']))}</td>"
        f"<td>{_attempts(i)}</td>"
        f"<td>{_when(i.get('created_at'))}</td>"
        f"<td>{_when(i.get('finished_at'))}</td>"
        f'<td class="dim">{escape(str(i["correlation_id"])[:8])}</td>'
        "</tr>"
        for i in items
    )
    return (
        '<table class="de-table"><thead><tr><th>Job</th><th>Type</th><th>State</th>'
        "<th>Attempts</th><th>Queued</th><th>Finished</th><th>Trace</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


# ---------------------------------------------------------------- job detail


def _strip(state: str) -> str:
    if state in ("failed", "timed_out", "canceled"):
        steps = ("queued", "running", state)
    else:
        steps = STRIP_STEPS
    parts = []
    for step in steps:
        cls = "now" if step == state else ("done" if steps.index(step) < steps.index(state) else "")
        parts.append(f'<span class="step {cls}">{escape(step)}</span>')
    return '<div class="de-strip">' + '<span class="sep">&rarr;</span>'.join(parts) + "</div>"


def _deadline(job: dict[str, Any], now: datetime | None = None) -> str:
    """Absolute deadline plus the budget left, ticking live in the browser."""
    raw = job.get("deadline_at")
    if not raw:
        return '<span class="text-comment">-</span>'
    try:
        moment = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return escape(str(raw))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    remaining = (moment - (now or datetime.now(UTC))).total_seconds()
    spent = remaining <= 0
    remaining_text = f"{abs(remaining) / 60:.0f}m {'over' if spent else 'left'}"
    css = "text-error" if spent else ("text-warning" if remaining < 60 else "text-success")
    return (
        f"{_when(moment)} "
        f'<span class="{css}" data-deadline="{int(moment.timestamp())}">'
        f"{escape(remaining_text)}</span>"
    )


def _cancel_action(job: dict[str, Any], theme: str) -> str:
    """A cancel control for live jobs only.

    The button is a form post to the UI route, not JavaScript: the action must work
    with scripting off, and the API route stays the single source of truth.
    """
    state = str(job.get("state"))
    if state not in CANCELLABLE_STATES:
        note = "cancel unavailable" if state in ACTIVE_STATES else "terminal state"
        return f'<span class="de-action-note text-comment">// {note}</span>'
    job_id = escape(str(job.get("id")))
    query = f"?theme={escape(theme)}" if theme else ""
    return (
        '<form class="de-action" method="post" '
        f'action="/ui/jobs/{job_id}/cancel{query}">'
        '<button type="submit" class="de-cancel">cancel job</button>'
        '<span class="de-action-note">'
        "queued &rarr; canceled // running &rarr; cancel_requested</span>"
        "</form>"
    )


def job_detail_page(job: dict[str, Any], theme: str) -> str:
    state = str(job.get("state"))
    facts = [
        ("job id", escape(str(job.get("id")))),
        ("type", escape(str(job.get("type")))),
        ("trace", escape(str(job.get("correlation_id")))),
        ("attempts", _attempts(job)),
        ("queued", _when(job.get("created_at"))),
        ("started", _when(job.get("started_at"))),
        ("finished", _when(job.get("finished_at"))),
        ("deadline", _deadline(job)),
    ]
    if job.get("error"):
        facts.append(("error", escape(str((job["error"] or {}).get("code", "see below")))))
    blocks = [
        '<section class="de-section"><h2>State</h2>'
        f"{_strip(state)}{_cancel_action(job, theme)}</section>",
        '<section class="de-section"><h2>Record</h2><dl class="de-kv">'
        + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
        + "</dl></section>",
        '<section class="de-section"><h2>Payload</h2>'
        f'<pre class="de-json">{escape(_pretty(job.get("payload")))}</pre></section>',
    ]
    if job.get("result"):
        blocks.append(
            '<section class="de-section"><h2>Result</h2>'
            f'<pre class="de-json">{escape(_pretty(job["result"]))}</pre></section>'
        )
    if job.get("error"):
        blocks.append(
            '<section class="de-section"><h2>Error</h2>'
            f'<pre class="de-json">{escape(_pretty(job["error"]))}</pre></section>'
        )
    episode = (job.get("result") or {}).get("episode_id")
    if episode:
        link = f"/api/v1/episodes/{escape(str(episode))}"
        blocks.append(f'<p><a href="{link}">// episode json</a></p>')
    return _page(
        "Job " + str(job.get("id"))[:8],
        "/ui/jobs",
        "".join(blocks),
        theme,
        _script(None, 0) + _DEADLINE_JS,
    )


# ---------------------------------------------------------------- artifacts


def artifacts_page(model: dict[str, Any], theme: str) -> str:
    body = (
        '<p class="de-sub">content addressed // poll 5s</p>'
        f"<div data-poll>{_artifacts_body(model)}</div>"
    )
    return _page("Artifacts", "/ui/artifacts", body, theme, _script("/ui/artifacts", 5000))


def artifacts_fragment(model: dict[str, Any]) -> str:
    return _artifacts_body(model)


def _artifacts_body(model: dict[str, Any]) -> str:
    items = model.get("items", [])
    if not items:
        return '<p class="de-empty">// no artifacts stored</p>'
    total = sum(int(a.get("size_bytes") or 0) for a in items)
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(a['hash'])[:24])}</code></td>"
        f'<td class="num">{_bytes(a.get("size_bytes"))}</td>'
        f"<td>{_when(a.get('created_at'))}</td>"
        f'<td class="num">{escape(str(len(a.get("episode_ids") or [])))}</td>'
        "</tr>"
        for a in items
    )
    return (
        f'<p class="de-sub">{len(items)} shown // {_bytes(total)} on this page</p>'
        '<table class="de-table"><thead><tr><th>Hash</th><th class="num">Size</th>'
        '<th>Written</th><th class="num">Episodes</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


# ---------------------------------------------------------------- metrics


def _ms(value: Any) -> str:
    try:
        return f"{float(value) * 1000:.1f}"
    except TypeError, ValueError:
        return "-"


# Traces worth a sparkline: what a data engine's operator watches. Anything not
# present in the window simply does not render.
TRACE_SERIES = (
    ("api_request_duration_seconds", "api latency"),
    ("jobs_run_time_seconds", "job run time"),
    ("pipeline_stage_duration_seconds", "stage duration"),
    ("catalog_query_duration_seconds", "catalog query"),
    ("jobs_queue_depth", "queue depth"),
)


def metrics_page(model: dict[str, Any], theme: str) -> str:
    body = (
        '<p class="de-sub">runtime telemetry // poll 5s // window from /api/v1/metrics</p>'
        f"<div data-poll>{_metrics_body(model)}</div>"
    )
    return _page("Metrics", "/ui/metrics", body, theme, _script("/ui/metrics", 5000))


def metrics_fragment(model: dict[str, Any]) -> str:
    return _metrics_body(model)


def _metrics_body(model: dict[str, Any]) -> str:
    summaries = model.get("summaries", [])
    series = model.get("series", {})
    depth = model.get("jobs_queue_depth", {})
    heartbeat = model.get("worker_heartbeat_age_seconds")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        by_name.setdefault(str(summary["name"]), []).append(summary)

    api = by_name.get("api_request_duration_seconds", [])
    runs = by_name.get("jobs_run_time_seconds", [])
    queries = by_name.get("catalog_query_duration_seconds", [])

    if heartbeat is None:
        heartbeat_cell = '<span class="text-comment">no heartbeat</span>'
    elif float(heartbeat) > 30:
        heartbeat_cell = f'<span class="text-error">{float(heartbeat):.0f}s stale</span>'
    else:
        heartbeat_cell = f'<span class="text-success">{float(heartbeat):.0f}s</span>'

    readouts = _readouts(
        [
            ("worker heartbeat", heartbeat_cell, ""),
            ("records", str(model.get("record_count", 0)), ""),
            ("queue depth", str(sum(int(v) for v in depth.values())), "jobs"),
            ("api p95", _p95(api), "ms"),
            ("run p95", _p95(runs), "ms"),
            ("query p95", _p95(queries), "ms"),
        ]
    )

    traces = "".join(
        '<section class="de-section"><h2>'
        + escape(label)
        + "</h2>"
        + _sparkline([float(p["v"]) for p in series.get(name, [])])
        + f'<p class="de-sub">{len(series.get(name, []))} buckets</p></section>'
        for name, label in TRACE_SERIES
        if series.get(name)
    )
    if not traces:
        traces = '<p class="de-empty">// no series in window</p>'

    api_rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(s['labels'].get('route', '-')))}</code></td>"
        f"<td>{escape(str(s['labels'].get('method', '-')))}</td>"
        f"<td>{escape(str(s['labels'].get('status_class', '-')))}</td>"
        f'<td class="num">{s["count"]}</td>'
        f'<td class="num">{_ms(s["p50"])}</td>'
        f'<td class="num">{_ms(s["p95"])}</td>'
        f'<td class="num">{_ms(s["p99"])}</td>'
        "</tr>"
        for s in sorted(api, key=lambda s: float(s["p95"]), reverse=True)
    )
    api_table = (
        '<section class="de-section"><h2>API latency by route</h2>'
        + (
            '<table class="de-table"><thead><tr><th>Route</th><th>Method</th><th>Status</th>'
            '<th class="num">Hits</th><th class="num">p50 ms</th><th class="num">p95 ms</th>'
            f'<th class="num">p99 ms</th></tr></thead><tbody>{api_rows}</tbody></table>'
            if api_rows
            else '<p class="de-empty">// no api traffic in window</p>'
        )
        + "</section>"
    )

    stage_rows = "".join(
        "<tr>"
        f"<td>{escape(str(s['labels'].get('stage', '-')))}</td>"
        f"<td>{escape(str(s['labels'].get('status', '-')))}</td>"
        f'<td class="num">{s["count"]}</td>'
        f'<td class="num">{_ms(s["p50"])}</td>'
        f'<td class="num">{_ms(s["p95"])}</td>'
        f"<td>{_meter(min(1.0, float(s['p95']) / 1.0), 10)}</td>"
        "</tr>"
        for s in sorted(
            by_name.get("pipeline_stage_duration_seconds", []),
            key=lambda s: float(s["p95"]),
            reverse=True,
        )
    )
    query_rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(s['labels'].get('operation', '-')))}</code></td>"
        f'<td class="num">{s["count"]}</td>'
        f'<td class="num">{_ms(s["p50"])}</td>'
        f'<td class="num">{_ms(s["p95"])}</td>'
        "</tr>"
        for s in sorted(queries, key=lambda s: float(s["p95"]), reverse=True)[:20]
    )
    tail = (
        '<section class="de-section"><h2>Stage + catalog latency</h2>'
        + (
            '<table class="de-table"><thead><tr><th>Stage</th><th>Status</th>'
            '<th class="num">Runs</th><th class="num">p50 ms</th><th class="num">p95 ms</th>'
            f"<th>Load</th></tr></thead><tbody>{stage_rows}</tbody></table>"
            if stage_rows
            else '<p class="de-empty">// no stages measured</p>'
        )
        + (
            '<table class="de-table"><thead><tr><th>Catalog op</th><th class="num">Calls</th>'
            '<th class="num">p50 ms</th><th class="num">p95 ms</th></tr></thead>'
            f"<tbody>{query_rows}</tbody></table>"
            if query_rows
            else ""
        )
        + "</section>"
    )
    return readouts + traces + api_table + tail


def _p95(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "-"
    return _ms(max(float(s["p95"]) for s in summaries))


# ---------------------------------------------------------------- episodes


def episodes_page(
    model: dict[str, Any], state_filter: str | None, flag: str | None, theme: str
) -> str:
    query = ""
    if state_filter or flag:
        pairs = [f"state={escape(state_filter or '')}", f"flag={escape(flag or '')}"]
        query = "?" + "&".join(pairs)
    body = (
        '<p class="de-sub">curation view // poll 5s</p>'
        '<form class="de-filters" method="get" action="/ui/episodes">'
        '<label for="state">state</label>'
        f'<select id="state" name="state">{_pick(EPISODE_STATES, state_filter)}</select>'
        '<label for="flag">flag</label>'
        f'<select id="flag" name="flag">{_pick(EPISODE_FLAGS, flag, empty="all")}</select>'
        '<button type="submit">apply</button></form>'
        f"<div data-poll>{_episodes_body(model)}</div>"
    )
    return _page("Episodes", "/ui/episodes", body, theme, _script("/ui/episodes" + query, 5000))


def episodes_fragment(model: dict[str, Any]) -> str:
    return _episodes_body(model)


def _pick(choices: tuple[str, ...], selected: str | None, empty: str = "all") -> str:
    return "".join(
        f'<option value="{c}"{" selected" if c == (selected or "") else ""}>{c or empty}</option>'
        for c in choices
    )


def _episodes_body(model: dict[str, Any]) -> str:
    items = model.get("items", [])
    if not items:
        return '<p class="de-empty">// no episodes match</p>'
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/episodes/{escape(str(i["id"]))}">{escape(str(i["id"])[:8])}</a></td>'
        f"<td>{escape(str(i.get('episode_key') or '-'))}</td>"
        f"<td>{escape(str(i.get('format') or '-'))}</td>"
        f"<td>{_state_badge(str(i.get('state') or 'ingested'))}</td>"
        f'<td class="num">{escape(str(i.get("frame_count") or "-"))}</td>'
        f'<td class="num">{_score(i.get("movement_score"))}</td>'
        f'<td class="num">{_score(i.get("jerk_score"))}</td>'
        f"<td>{_stall_cell(i)}</td>"
        f"<td>{_verdict_cell(i)}</td>"
        f"<td>{_when(i.get('created_at'))}</td>"
        "</tr>"
        for i in items
    )
    return (
        '<table class="de-table"><thead><tr><th>Episode</th><th>Key</th><th>Format</th>'
        '<th>State</th><th class="num">Frames</th><th class="num">Move</th>'
        '<th class="num">Jerk</th><th>Stall</th><th>Verdict</th><th>Ingested</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _score(value: Any) -> str:
    if value is None:
        return "-"
    # Published channel stats can be per-dimension lists; show the head, not the list.
    if isinstance(value, list | tuple):
        return _score(value[0]) + ("&hellip;" if len(value) > 1 else "")
    try:
        return f"{float(value):.4f}"
    except TypeError, ValueError:
        return escape(str(value))


def _stall_cell(item: dict[str, Any]) -> str:
    value = item.get("stall_ratio")
    return "-" if value is None else _meter(_bounded(value), 10)


def _verdict_cell(item: dict[str, Any]) -> str:
    verdict = item.get("verdict")
    return _verdict_badge(verdict) if verdict else '<span class="text-comment">-</span>'


def _bounded(value: Any, ceiling: float = 1.0) -> float:
    try:
        return max(0.0, min(ceiling, float(value)))
    except TypeError, ValueError:
        return 0.0


# ---------------------------------------------------------------- episode detail


def episode_detail_page(episode: dict[str, Any], quality: dict[str, Any] | None, theme: str) -> str:
    meta = episode.get("metadata") or {}
    facts = [
        ("episode id", escape(str(episode.get("id")))),
        ("key", escape(str(episode.get("episode_key") or "-"))),
        ("format", escape(str(episode.get("format") or meta.get("format") or "-"))),
        ("state", _state_badge(str(episode.get("state") or "ingested"))),
        ("robot", escape(str(meta.get("robot") or "-"))),
        ("task", escape(str(meta.get("task") or "-"))),
        ("frames", escape(str(meta.get("frame_count") or "-"))),
        ("duration", f"{float(meta.get('duration_seconds') or 0):.2f} s"),
        ("fps", escape(str(meta.get("fps") or "-"))),
    ]
    blocks = [
        '<section class="de-section"><h2>Record</h2><dl class="de-kv">'
        + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
        + "</dl></section>"
    ]
    blocks.append(_quality_section(quality))
    channels = meta.get("channel_stats") or {}
    if channels:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(str(name))}</code></td>"
            f'<td class="num">{escape(str(c.get("count") or "-"))}</td>'
            f'<td class="num">{_score(c.get("min"))}</td>'
            f'<td class="num">{_score(c.get("max"))}</td>'
            f'<td class="num">{_score(c.get("mean"))}</td>'
            f'<td class="num">{_score(c.get("std"))}</td>'
            "</tr>"
            for name, c in sorted(channels.items())
        )
        blocks.append(
            '<section class="de-section"><h2>Channels</h2>'
            '<table class="de-table"><thead><tr><th>Channel</th><th class="num">n</th>'
            '<th class="num">min</th><th class="num">max</th><th class="num">mean</th>'
            f'<th class="num">std</th></tr></thead><tbody>{rows}</tbody></table></section>'
        )
    blocks.append(
        '<section class="de-section"><h2>Metadata</h2>'
        f'<pre class="de-json">{escape(_pretty(meta))}</pre></section>'
    )
    return _page(
        "Episode " + str(episode.get("id"))[:8],
        "/ui/episodes",
        "".join(blocks),
        theme,
        _script(None, 0),
    )


def _quality_section(quality: dict[str, Any] | None) -> str:
    if not quality:
        return (
            '<section class="de-section"><h2>Motion quality</h2>'
            '<p class="de-empty">// no quality signals for this episode</p></section>'
        )
    readouts = _readouts(
        [
            ("verdict", _verdict_badge(quality.get("verdict")), ""),
            ("movement", _score(quality.get("movement_score")), "/frame"),
            ("jerk", _score(quality.get("jerk_score")), "norm"),
            (
                "stall",
                _meter(_bounded(quality.get("stall_ratio")), 12),
                "",
            ),
            ("length z", _score(quality.get("length_zscore")), "sigma"),
            ("frames", escape(str(quality.get("frame_count") or "-")), ""),
        ]
    )
    dims = quality.get("dims") or []
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(d.get('name')))}</code></td>"
        f"<td>{'act' if d.get('active') else 'idle'}</td>"
        f"<td>{'disc' if d.get('discrete') else '-'}</td>"
        f"<td>{'grip' if d.get('gripper') else '-'}</td>"
        f"<td>{_meter(_bounded(d.get('norm_delta_std'), 0.25), 12)}</td>"
        f'<td class="num">{_score(d.get("mean_abs_delta_norm"))}</td>'
        "</tr>"
        for d in dims
    )
    table = ""
    if rows:
        table = (
            '<table class="de-table"><thead><tr><th>Dim</th><th>Mode</th><th>Kind</th>'
            '<th>Grip</th><th>sigma(d)/range</th><th class="num">mean|d|/range</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    return '<section class="de-section"><h2>Motion quality</h2>' + readouts + table + "</section>"


# ---------------------------------------------------------------- insights


def insights_page(model: dict[str, Any], theme: str) -> str:
    body = (
        '<p class="de-sub">dataset curation // poll 10s // /api/v1/quality/summary</p>'
        f"<div data-poll>{_insights_body(model)}</div>"
    )
    return _page("Insights", "/ui/insights", body, theme, _script("/ui/insights", 10000))


def insights_fragment(model: dict[str, Any]) -> str:
    return _insights_body(model)


def _insights_body(model: dict[str, Any]) -> str:
    verdicts = model.get("verdicts", {})
    length = model.get("length", {})
    readouts = _readouts(
        [
            ("episodes", str(model.get("episode_count", 0)), "scored"),
            ("smooth", f'<span class="text-success">{verdicts.get("smooth", 0)}</span>', ""),
            ("moderate", f'<span class="text-warning">{verdicts.get("moderate", 0)}</span>', ""),
            ("jerky", f'<span class="text-error">{verdicts.get("jerky", 0)}</span>', ""),
            ("unknown", f'<span class="text-comment">{verdicts.get("unknown", 0)}</span>', ""),
            ("length mean", _score(length.get("mean")), "frames"),
            ("length spread", _score(length.get("std")), "sigma"),
        ]
    )

    length_section = (
        '<section class="de-section"><h2>Episode lengths</h2>'
        + _histogram_svg(length.get("histogram") or [])
        + f'<p class="de-sub">min {escape(str(length.get("min", "-")))}'
        f" // max {escape(str(length.get('max', '-')))} frames</p></section>"
    )

    speed = model.get("speed_distribution", [])
    speed_section = (
        '<section class="de-section"><h2>Speed distribution</h2>'
        + (_scatter_svg(speed, "movement_score") if speed else '<p class="de-empty">// no data</p>')
        + f'<p class="de-sub">{len(speed)} episodes // movement per frame</p></section>'
    )

    matrix = model.get("heat_matrix", {})
    dims = (matrix.get("dims") or [])[:16]
    heat = ""
    if dims:
        episodes = (matrix.get("episodes") or [])[:20]
        peak = (
            max(
                (v for e in episodes for v in (e.get("values") or []) if v is not None),
                default=0.0,
            )
            or 1.0
        )
        head = "".join(f"<th>{escape(str(d))}</th>" for d in dims)
        body_rows = ""
        for e in episodes:
            cells = ""
            for index, d in enumerate(dims):
                values = e.get("values") or []
                value = values[index] if index < len(values) else None
                if value is None:
                    cells += '<td class="de-heat">·</td>'
                else:
                    cells += (
                        f'<td class="de-heat" style="--v:{float(value) / peak:.3f}" '
                        f'title="{escape(str(d))} = {float(value):.4f}"></td>'
                    )
            body_rows += (
                f'<tr><td><a href="/ui/episodes/{escape(str(e["episode_id"]))}">'
                f"{escape(str(e['episode_id'])[:8])}</a></td>{cells}</tr>"
            )
        heat = (
            '<section class="de-section"><h2>Cross-episode variance // sigma(d)/range</h2>'
            '<div class="de-heat-wrap"><table class="de-table de-heat-table"><thead><tr><th>Ep</th>'
            f"{head}</tr></thead><tbody>{body_rows}</tbody></table></div>"
            '<p class="de-sub">brighter = rougher // hover for values</p></section>'
        )

    outliers = model.get("outliers", {})
    lists = "".join(
        '<div><h3 class="de-outlier-h">'
        + escape(title)
        + '</h3><ul class="de-outliers">'
        + "".join(
            f'<li><a href="/ui/episodes/{escape(str(o.get("episode_id")))}">'
            f"{escape(str(o.get('episode_id'))[:8])}</a> "
            f'<span class="dim">{escape(str(o.get("episode_key") or ""))}</span> '
            f'<span class="num">{_score(o.get("value"))}</span> '
            f"{_verdict_badge(o.get('verdict'))}</li>"
            for o in items
        )
        + "</ul></div>"
        for title, items in (
            ("top jerk", outliers.get("jerk", [])),
            ("top stall", outliers.get("stall", [])),
            ("length outliers", outliers.get("length", [])),
        )
    )
    outlier_section = (
        '<section class="de-section"><h2>Outliers // removal candidates</h2>'
        + (
            f'<div class="de-outlier-grid">{lists}</div>'
            if lists
            else '<p class="de-empty">// no data</p>'
        )
        + "</section>"
    )
    return readouts + length_section + speed_section + heat + outlier_section
