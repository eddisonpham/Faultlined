"""Minimal server-rendered UI for the MVP Status / Jobs / Artifacts slice.

Per ADR 0014 this is plain server-rendered HTML plus vanilla-JS polling: no
framework, no bundler, no new runtime dependency. The pages read only the
application's own HTTP surface and never touch PostgreSQL or the artifact store
directly. Visual design is still deferred to the phase-12 pass; this establishes
mechanism and information, not style.

Polling re-requests the same page with ``?fragment=1`` and swaps the returned
HTML. The alternative, re-rendering JSON in JavaScript, would mean writing every
row twice; the fragment keeps Python as the only renderer.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

STYLESHEET = Path(__file__).with_name("faultlined.css")
STYLESHEET_URL = "/ui/faultlined.css"

NAV_LINKS = (
    ("/", "Status"),
    ("/ui/jobs", "Jobs"),
    ("/ui/artifacts", "Artifacts"),
    ("/docs", "API"),
)


def stylesheet() -> str:
    return STYLESHEET.read_text(encoding="utf-8")


def _nav(active: str) -> str:
    links = " ".join(
        f'<a href="{href}"{' class="on"' if href == active else ""}>{label}</a>'
        for href, label in NAV_LINKS
    )
    return f"<nav>{links}</nav>"


def _page(title: str, active: str, body: str, script: str = "") -> str:
    head = '<link rel="stylesheet" href="' + STYLESHEET_URL + '">'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(title)} - Faultlined</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{head}</head><body>{_nav(active)}"
        f"<h1>{escape(title)}</h1>{body}"
        f"<script>{script}</script></body></html>"
    )


def _bytes(value: Any) -> str:
    if value is None:
        return '<span class="muted">n/a</span>'
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _state(state: str) -> str:
    css = {"succeeded": "s", "failed": "f", "canceled": "q", "timed_out": "c"}.get(state, "")
    return f'<span class="{css}">{escape(state)}</span>' if css else escape(state)


def _when(value: Any) -> str:
    if not value:
        return '<span class="muted">-</span>'
    return escape(str(value))[:19].replace("T", " ")


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)[:4000]
    except TypeError, ValueError:
        return str(value)


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


def _poll_js(url: str, ms: int) -> str:
    return _POLL_JS % (json.dumps(url), ms)


def status_page(initial: dict[str, Any]) -> str:
    body = _status_body(initial)
    return _page(
        "Status",
        "/",
        '<p class="sub">Health, queue depth, and host telemetry. Refreshes every 3 s.</p>'
        f"<div data-poll>{body}</div>",
        _poll_js("/ui", 3000),
    )


def status_fragment(data: dict[str, Any]) -> str:
    return _status_body(data)


def _status_body(data: dict[str, Any]) -> str:
    res = data.get("resources", {})
    depth = data.get("queue_depth", {})
    active = sum(depth.get(k, 0) for k in ("queued", "running", "retrying", "cancel_requested"))
    gpu = res.get("gpu_model") or ("present" if res.get("gpu_present") else "none")
    cards = [
        ("Health", '<span class="s">ok</span>'),
        ("Active jobs", str(active)),
        ("Artifacts", str(data.get("artifact_count", 0))),
        ("Episodes", str(data.get("episode_count", 0))),
        ("CPU", f"{res['cpu_percent']}%" if res.get("cpu_percent") is not None else "n/a"),
        ("Memory", _bytes(res.get("memory_used_bytes"))),
        ("Disk free", _bytes(res.get("disk_free_bytes"))),
        ("GPU", escape(str(gpu))),
    ]
    grid = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in cards
    )
    rows = "".join(f"<tr><td>{escape(str(k))}</td><td>{v}</td></tr>" for k, v in depth.items())
    return (
        f'<div class="grid">{grid}</div><h2>Queue depth by state</h2>'
        f"<table><thead><tr><th>State</th><th>Jobs</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def jobs_page(initial: dict[str, Any], state_filter: str | None) -> str:
    query = f"&state={state_filter}" if state_filter else ""
    fragment_url = "/ui/jobs" + (f"?state={state_filter}" if state_filter else "")
    body = (
        '<p class="sub">Newest first. Refreshes every 3 s.</p>'
        f'<form method="get" action="/ui/jobs"><label for="s">State</label>'
        '<select id="s" name="state" onchange="this.form.submit()">'
        f"{_options(state_filter)}</select>"
        "</form>"
        f"<div data-poll>{_jobs_body(initial)}</div>"
    )
    del query
    return _page("Jobs", "/ui/jobs", body, _poll_js(fragment_url, 3000))


def jobs_fragment(initial: dict[str, Any]) -> str:
    return _jobs_body(initial)


def _options(state_filter: str | None) -> str:
    states = ("", "queued", "running", "succeeded", "failed", "canceled", "timed_out")
    return "".join(
        f'<option value="{s}"{" selected" if s == state_filter else ""}>{s or "all"}</option>'
        for s in states
    )


def _jobs_body(initial: dict[str, Any]) -> str:
    rows = (
        "".join(
            "<tr>"
            f'<td><a href="/ui/jobs/{escape(str(i["id"]))}">'
            f"<code>{escape(str(i['id'])[:8])}</code></a></td>"
            f"<td>{escape(str(i['type']))}</td>"
            f"<td>{_state(str(i['state']))}</td>"
            f"<td>{_when(i.get('created_at'))}</td>"
            f"<td>{_when(i.get('finished_at'))}</td>"
            f'<td class="muted"><code>{escape(str(i["correlation_id"])[:8])}</code></td>'
            "</tr>"
            for i in initial.get("items", [])
        )
        or '<tr><td colspan="6" class="muted">No jobs yet.</td></tr>'
    )
    return (
        "<table><thead><tr><th>Job</th><th>Type</th><th>State</th>"
        f"<th>Created</th><th>Finished</th><th>Correlation</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def job_detail_page(job: dict[str, Any]) -> str:
    timeline = " &rarr; ".join(
        f"{label}: <code>{_when(job.get(field))}</code>"
        for label, field in (
            ("queued", "created_at"),
            ("started", "started_at"),
            ("finished", "finished_at"),
        )
    )
    blocks = [
        f"<h2>Timeline</h2><p>{timeline}</p>",
        "<h2>Payload</h2><pre>" + escape(_pretty(job.get("payload"))) + "</pre>",
    ]
    if job.get("result"):
        blocks.append("<h2>Result</h2><pre>" + escape(_pretty(job["result"])) + "</pre>")
    if job.get("error"):
        blocks.append("<h2>Error</h2><pre>" + escape(_pretty(job["error"])) + "</pre>")
    episode = (job.get("result") or {}).get("episode_id")
    if episode:
        link = f"/api/v1/episodes/{escape(str(episode))}"
        blocks.append(f'<p><a href="{link}">Episode JSON &rarr;</a></p>')
    body = (
        f'<p class="sub">{_state(str(job.get("state")))} - type '
        f"<code>{escape(str(job.get('type')))}</code> - correlation "
        f"<code>{escape(str(job.get('correlation_id')))}</code></p>" + "".join(blocks)
    )
    return _page("Job " + str(job.get("id"))[:8], "/ui/jobs", body)


def artifacts_page(initial: dict[str, Any]) -> str:
    body = (
        '<p class="sub">Content-addressed blobs, newest first. Refreshes every 5 s.</p>'
        f"<div data-poll>{_artifacts_body(initial)}</div>"
    )
    return _page("Artifacts", "/ui/artifacts", body, _poll_js("/ui/artifacts", 5000))


def artifacts_fragment(initial: dict[str, Any]) -> str:
    return _artifacts_body(initial)


def _artifacts_body(initial: dict[str, Any]) -> str:
    rows = (
        "".join(
            "<tr>"
            f"<td><code>{escape(str(a['hash'])[:16])}...</code></td>"
            f"<td>{_bytes(a.get('size_bytes'))}</td>"
            f"<td>{_when(a.get('created_at'))}</td>"
            f'<td class="muted">{escape(str(len(a.get("episode_ids") or [])))}</td>'
            "</tr>"
            for a in initial.get("items", [])
        )
        or '<tr><td colspan="4" class="muted">No artifacts yet.</td></tr>'
    )
    return (
        "<table><thead><tr><th>Hash</th><th>Size</th><th>Created</th>"
        f"<th>Episodes</th></tr></thead><tbody>{rows}</tbody></table>"
    )
