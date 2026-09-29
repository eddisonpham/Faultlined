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
    ("/ui/artifacts", "Artifacts"),
    ("/docs", "API"),
)

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
