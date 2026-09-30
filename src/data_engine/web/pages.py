"""Server-rendered UI for the MVP Status / Jobs / Artifacts slice.

Per ADR 0014 this is server-rendered HTML with vanilla-JS polling: no framework,
no bundler, no build step, and no JavaScript dependency. The terminal/instrument
vocabulary comes from a vendored stylesheet (see vendor/terminal-ui/NOTICE.md);
this module owns layout, semantics, and escaping.

Polling re-requests the same page with ``X-Fragment: 1`` and swaps the returned
HTML. The alternative, re-rendering JSON in JavaScript, would mean writing every
row twice and two renderers eventually disagreeing.

Every page function here must survive being handed a half-populated model. The
data comes from a live catalog that can be mid-ingest, and a page that 500s
because a row lacks a field is worse than one that renders a dash. See
``agents/architecture/frontend.md``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
VENDOR = HERE / "vendor" / "terminal-ui"
CORE_URL = "/ui/vendor/terminal-ui/core.css"
THEME_URL = "/ui/vendor/terminal-ui/theme-{name}.css"
LAYOUT_URL = "/ui/faultlined.css"
APP_JS_URL = "/ui/app.js"

# Upstream ships more themes; these four are vendored. See vendor/terminal-ui/NOTICE.md.
THEMES = ("vt220", "amber", "github-dark", "monochrome")
DEFAULT_THEME = "vt220"

# (href, label, number-key). The key is printed in the nav and handled by
# app.js; keeping the two in one tuple means a link cannot advertise a shortcut
# the runtime does not implement. Ordered by how often an operator looks, not by
# alphabet - Status first because it is the landing page, Incidents second
# because a night-time notifier is the reason to come back to this UI.
NAV_LINKS = (
    ("/ui", "Status", "1"),
    ("/ui/incidents", "Incidents", "2"),
    ("/ui/jobs", "Jobs", "3"),
    ("/ui/episodes", "Episodes", "4"),
    ("/ui/failures", "Failures", "5"),
    ("/ui/slices", "Slices", "6"),
    ("/ui/insights", "Insights", "7"),
    ("/ui/metrics", "Metrics", "8"),
    ("/ui/artifacts", "Artifacts", "9"),
    ("/ui/schema", "Schema", "0"),
    ("/ui/builds", "Builds", "b"),
)

FONT_URL = "/ui/vendor/departure-mono/DepartureMono-Regular.woff2"
FONT_PATH = HERE / "vendor" / "departure-mono" / "DepartureMono-Regular.woff2"

# Curation views on the Episodes page; the vocabulary and its SQL predicates live
# in data_engine.curation so the API, the catalog, and a saved slice cannot drift.
from data_engine.curation import EPISODE_FLAGS, EPISODE_STATES  # noqa: E402
from data_engine.web.charts import (  # noqa: E402
    PAD_LEFT,
    PAD_RIGHT,
    axis_ticks,
    bar_chart,
    choose_scale,
    format_value,
    line_chart,
)

__all__ = ["EPISODE_FLAGS", "EPISODE_STATES"]

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


def app_script() -> str:
    """The client runtime. Read per request like the CSS, so an edit is live."""
    return (HERE / "app.js").read_text(encoding="utf-8")


def layout_css() -> str:
    return (HERE / "faultlined.css").read_text(encoding="utf-8")


def theme_or_default(name: str | None) -> str:
    """Resolve an untrusted ``?theme=`` value to a vendored theme name.

    Public because the error handlers need it: a 500 page has to be rendered in
    the theme the operator had selected, and an unknown name must fall back
    rather than emit a ``<link>`` to a stylesheet that does not exist.
    """
    return name if name in THEMES else DEFAULT_THEME


def _nav(active: str, theme: str) -> str:
    links = "".join(
        f'<a href="{href}" data-key="{key}" class="fine-use-focusable"'
        f"{' aria-current="page"' if href == active else ''}>{label}</a>"
        for href, label, key in NAV_LINKS
    )
    options = "".join(
        f'<option value="{t}"{" selected" if t == theme else ""}>{t}</option>' for t in THEMES
    )
    return (
        '<nav class="de-nav fine-use-focusable" aria-label="Sections">'
        '<a class="de-brand" href="/ui">Faultlined'
        "<span> / data engine</span></a>"
        f"{links}"
        '<span class="de-spacer"></span>'
        # Link health. Hidden by default: an unchanging "live" badge in the
        # corner of every page reads as branding, not as information. app.js
        # reveals it the moment the state is anything but healthy, which is the
        # only time it carries news. The word is still always present, so the
        # state is never conveyed by hue alone.
        '<span class="de-led" data-led data-state="ok" role="status" hidden>'
        "<span data-led-text></span></span>"
        # Hidden until app.js confirms it can switch the stylesheet; the form
        # submit below is the no-JS path and stays in the markup.
        f'<form method="get" action="{escape(active)}" class="de-theme-form">'
        '<select class="de-theme theme-dropdown fine-use-focusable" name="theme" '
        f'data-theme-select hidden onchange="this.form.submit()">{options}</select>'
        '<button type="submit" class="de-sr">Apply theme</button></form>'
        "</nav>"
    )


def _banner() -> str:
    """Failure strip. Server-rendered hidden; app.js reveals it.

    The single most important error state in this UI: a poll that silently
    fails leaves yesterday's numbers on screen looking authoritative.
    """
    return (
        '<div class="de-banner" data-banner data-visible="0" role="alert">'
        '<span class="de-banner-mark" aria-hidden="true">!</span>'
        '<span class="de-banner-msg" data-banner-msg></span>'
        '<button type="button" data-banner-retry>retry now</button>'
        "</div>"
    )


def _poll_attrs(url: str | None, ms: int) -> str:
    """Attributes app.js reads to drive a panel. Empty string disables polling."""
    if not url:
        return ""
    return f' data-poll="{escape(url)}" data-poll-ms="{ms}"'


def _live(url: str | None, ms: int, html: str) -> str:
    """Wrap the volatile part of a page in the region app.js replaces.

    Deliberately the *innermost* element rather than ``<main>``: a poll that
    replaces the whole main region would also wipe the filter forms and the
    panel headings around it, so a filtered Jobs page would quietly lose its
    filter after three seconds. The fragment returned by the route must be
    byte-identical in shape to what sits inside this element.
    """
    return f'<div class="de-live"{_poll_attrs(url, ms)}>{html}</div>'


def _sweep() -> str:
    """Navigation indicator: a spring trace shown only while a page is in flight.

    Server-rendered hidden. There is deliberately no page-load spinner: the
    document is fully rendered by the time any script runs, so a loading panel
    on load is a panel sitting above content that is already there - it claimed
    work that was never outstanding. This appears only when a link click starts
    a real navigation, which is the one case where the wait is real.
    """
    return (
        '<div class="de-sweep-panel" data-sweep-panel hidden aria-hidden="true">'
        '<div class="de-sweep" data-sweep><span></span><span></span><span></span></div>'
        "</div>"
    )


def _page(
    title: str,
    active: str,
    body: str,
    theme: str,
) -> str:
    """Assemble a full document. ``body`` is the already-composed page content."""
    subtitle = _SUBTITLES.get(active, "")
    sub = f'<p class="de-sub">{escape(subtitle)}</p>' if subtitle else ""
    return (
        '<!doctype html><html lang="en" data-theme="' + theme + '">'
        '<head><meta charset="utf-8">'
        f"<title>{escape(title)} // Faultlined</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f'<link rel="stylesheet" href="{CORE_URL}">'
        f'<link rel="stylesheet" data-de-theme href="{THEME_URL.format(name=theme)}">'
        f'<link rel="stylesheet" href="{LAYOUT_URL}">'
        f'<script src="{APP_JS_URL}" defer></script>'
        "</head>"
        # fine-use-app is the vendored hook the CRT themes hang their scanline
        # and background off, so the shell carries it rather than <body>.
        f'<body><a class="de-skip fine-use-focusable" href="#main">Skip to content</a>'
        f"{_sweep()}"
        f'<div class="de-shell fine-use-app">{_nav(active, theme)}'
        f"{_banner()}"
        f'<div class="de-head"><h1 class="fine-use-h1">{escape(title)}</h1>'
        '<span class="de-clock" data-clock data-stale="0">--:--:--</span></div>'
        f"{sub}"
        f'<main id="main" tabindex="-1">{body}</main>'
        '<span class="de-sr" role="status" aria-live="polite" data-live></span>'
        "</div></body></html>"
    )


def error_page(status: int, detail: str, theme: str | None = None) -> str:
    """A failure the operator has to read, rendered in the same instrument skin.

    A raw JSON 500 on a page the browser navigated to is a dead end; this says
    what happened, gives the correlation id to quote, and offers a way back.

    ``theme`` is resolved through the same allowlist as every other page, so a
    caller cannot emit a ``<link>`` to a stylesheet that does not exist. The
    error path is the last thing standing between a bad query string and a page
    that renders with no stylesheet at all.
    """
    return _page(
        f"Error {status}",
        "",
        _section(
            f"Request failed // {status}",
            f'<p class="de-empty">{escape(detail)}</p>'
            '<p class="de-sub">this is usually transient: the catalog may be '
            "starting, or a job is holding a lock.</p>"
            '<form method="get" action=""><button type="submit" '
            'class="de-cancel fine-use-focusable">retry</button></form>',
            "the panel did not load",
        ),
        theme_or_default(theme),
    )


# One line of context per section, so the operator knows what a panel is and how
# fresh it is without leaving the page. Deliberately describes the *work*, never
# the transport: no route names, no verbs, no endpoint paths. The browser surface
# is an operator console, and a route template rendered as a heading tells the
# reader nothing they can act on while disclosing the shape of the surface.
_SUBTITLES = {
    "/ui": "live telemetry // poll 3s",
    "/ui/jobs": "queue and run history // newest first",
    "/ui/incidents": "deterministic notifier // read-only",
    "/ui/episodes": "curation view // quality scored at ingest",
    "/ui/failures": "quarantine triage // read-only",
    "/ui/slices": "named curation filters // membership recomputed on read",
    "/ui/insights": "dataset distribution // outliers are removal candidates",
    "/ui/metrics": "runtime telemetry // where the time goes",
    "/ui/artifacts": "content addressed // hash is the identity",
    "/ui/schema": "live catalog // read from the database, not a drawing",
    "/ui/builds": "content addressed // the hash is the identity",
}


# ---------------------------------------------------------------- primitives


def _readouts(pairs: list[tuple[str, str, str]]) -> str:
    """A row of instrument readouts. ``pairs`` is (legend, value-html, unit)."""
    cells = "".join(
        f'<div class="de-readout block-item"><dt>{escape(label)}</dt>'
        f'<dd class="current-value">{value}<small>{escape(unit)}</small></dd></div>'
        for label, value, unit in pairs
    )
    return f'<dl class="de-readouts">{cells}</dl>'


def _meter(fraction: float, width: int = 24) -> str:
    """ASCII meter, e.g. [#########.............] 38%.

    Kept as text rather than a styled div: it survives a print, a screen reader,
    and a stylesheet that failed to load, and it needs no ARIA to be legible.
    """
    filled = max(0, min(width, round(fraction * width)))
    return (
        f'<span class="de-meter" role="img" '
        f'aria-label="{round(fraction * 100)} percent">[{"#" * filled}'
        f'<span class="off">{"." * (width - filled)}</span>] '
        f"{round(fraction * 100)}%</span>"
    )


def _copyable(value: str, shown: str | None = None) -> str:
    """An identifier the operator can click to copy in full.

    Tables truncate ids to keep columns narrow, and a truncated id pasted into
    a bug report is worse than no id. Shift-click still selects the text.
    """
    full = escape(str(value))
    text = escape(str(shown if shown is not None else value))
    return f'<code class="de-copy" data-copy="{full}" title="click to copy">{text}</code>'


def _episode_dots(items: list[dict[str, Any]], key: str, *, width: int = 720) -> str:
    """One labelled, linked dot per episode on a shared scale.

    The previous revision was a bare `<circle>` per episode with no axis, so a
    reader could count the dots and nothing else. Every dot here is a link to the
    episode, carries its value in a `<title>`, and sits on an axis whose bounds
    are computed from the data by `web.charts` - including the automatic switch
    to a log axis, which is what keeps a wide spread from flattening into a line.
    """
    if not items:
        return _empty("no scored episodes", "ingest something to populate this")
    values = [float(item.get(key) or 0.0) for item in items]
    scale = choose_scale(values)
    top, bottom = 12.0, 96.0
    step = (width - PAD_LEFT - PAD_RIGHT) / max(len(items), 1)
    dots = []
    for index, (item, value) in enumerate(zip(items, values, strict=True)):
        x = PAD_LEFT + step * (index + 0.5)
        y = scale.pixel(value, bottom, top)
        episode_id = str(item.get("episode_id") or "")
        label = (
            f"episode {episode_id[:8]}: {format_value(value)} ({item.get('verdict', 'unknown')})"
        )
        dots.append(
            f'<a href="/ui/episodes/{escape(episode_id)}" class="de-dot-link">'
            f'<circle class="de-point" cx="{x:.1f}" cy="{y:.1f}" r="4">'
            f"<title>{escape(label)}</title></circle></a>"
        )
    described = (
        f"{len(items)} episodes by {key}, "
        f"{format_value(scale.lo)} to {format_value(scale.hi)}"
        f"{' log10' if scale.logarithmic else ''}"
    )
    grid = "".join(
        f'<line class="de-grid" x1="{PAD_LEFT}" y1="{scale.pixel(tick, bottom, top):.1f}" '
        f'x2="{width - PAD_RIGHT}" y2="{scale.pixel(tick, bottom, top):.1f}"/>'
        f'<text class="de-tick" x="{PAD_LEFT - 6}" '
        f'y="{scale.pixel(tick, bottom, top) + 3:.1f}" text-anchor="end">'
        f"{escape(format_value(tick))}</text>"
        for tick in axis_ticks(scale)
    )
    return (
        f'<svg class="de-plot de-plot-dots" viewBox="0 0 {width} 130" role="img" '
        f'aria-label="{escape(described)}" preserveAspectRatio="xMidYMid meet">'
        + grid
        + "".join(dots)
        + "</svg>"
    )


def _integrity_badge(integrity: Any) -> str:
    """`ok` / `gapped` / `unknown`, as a word - never colour alone."""
    css = {"ok": "success", "gapped": "error"}.get(str(integrity), "comment")
    return f'<span class="text-{css}">{escape(str(integrity or "unknown"))}</span>'


def _verdict_badge(verdict: Any) -> str:
    css = {"smooth": "success", "moderate": "warning", "jerky": "error"}.get(
        str(verdict), "comment"
    )
    return f'<span class="text-{css}">{escape(str(verdict))}</span>'


def _svg_open(width: int, height: int, label: str, extra: str = "") -> str:
    """Scope-screen frame.

    ``role="img"`` with a real label rather than a decorative trace: a
    sparkline is data, and a screen reader should be told it is a chart rather
    than skip an empty element.
    """
    return (
        f'<svg class="de-chart{extra}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="{escape(label)}">'
    )


def _sparkline(values: list[float], *, width: int = 260, height: int = 44, label: str = "") -> str:
    """SVG trace of a series on the graticule; one `<polyline>`, nothing fetched."""
    data = [float(v) for v in values] or [0.0]
    lo, hi = min(data), max(data)
    span = (hi - lo) or 1.0
    step = width / max(len(data) - 1, 1)
    points = " ".join(
        f"{i * step:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}"
        for i, v in enumerate(data)
    )
    span_text = f", range {lo:.4g} to {hi:.4g}" if label else ""
    return (
        _svg_open(width, height, f"{label or 'trend'}: {len(data)} samples{span_text}")
        + f'<polyline points="{points}"/></svg>'
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
        _svg_open(width, height, f"histogram of {len(bins)} bins", " de-chart-bars")
        + f"{rects}</svg>"
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
    return _svg_open(width, height, f"{len(items)} episodes by {key}") + f"{dots}</svg>"


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


def _table(caption: str, headers: str, rows: str, extra: str = "", empty: str | None = None) -> str:
    """A data table with a real caption, inside a scroll container.

    The caption is visually hidden but present: a table of job states with no
    accessible name is a grid of anonymous cells to a screen reader, and this
    UI is meant to be usable without sight.

    The wrapper is what stops a wide table from pushing a horizontal scrollbar
    onto the whole page. The Episodes table is ten columns and does overflow at
    laptop width; without this the entire document scrolls sideways, including
    the nav. ``tabindex="0"`` makes the scroll region keyboard-reachable, which
    is required or a keyboard user cannot pan it at all.
    """
    if not rows:
        return _empty(empty if empty is not None else caption)
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" '
        f'aria-label="{escape(caption)}">'
        f'<table class="de-table fine-use-data-table{extra}">'
        f'<caption class="de-sr">{escape(caption)}</caption>'
        f"<thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>"
        "</div>"
    )


def _empty(what: str, hint: str = "", link: tuple[str, str] | None = None) -> str:
    """Empty state.

    Says *what is absent* and, where there is one, *what would make it appear*.
    A bare blank panel reads as a broken page; "// no jobs recorded" reads as a
    fact about the system.

    `link` is a (label, href) pair rendered as real markup. It is a separate
    argument rather than a flag on `hint` because `hint` is escaped: a hint that
    quietly stopped escaping would be a hole, and one that escaped an author's
    <a> would be a broken link. Two arguments, two rules, no third way.
    """
    tail = f" // {escape(hint)}" if hint else ""
    if link:
        label, href = link
        tail += f' // <a href="{escape(href)}">{escape(label)}</a>'
    return f'<p class="de-empty">// {escape(what)}{tail}</p>'


def _section(title: str, body: str, label: str = "") -> str:
    """A bolted-down panel: heading, optional silkscreen label, content."""
    bezel = f'<span class="de-bezel-label">{escape(label)}</span>' if label else ""
    return (
        '<section class="de-section fine-use-component">'
        f"<h2>{escape(title)}{bezel}</h2>{body}</section>"
    )


def _text(value: Any) -> str:
    """A plain string, never None. Used where a helper is chained onto the result."""
    return "" if value is None else str(value)


# ------------------------------------------------------------------ ingest form

# The two shapes an ingest can take, in the words a person would use. The
# underlying job types are `ingest` (an episode in the request) and
# `ingest_source` (a path on disk); those names are for the code.
_INGEST_KINDS = (
    ("episode", "an episode"),
    ("path", "a dataset on disk"),
)

_INGEST_STEPS = (
    ("Describe the episode", "Name the task and robot. Frames are generated as a ramp."),
    ("Or point at a path", "A LeRobot dataset directory. The format is detected on read."),
    ("Watch it land", "Jobs shows the queue. Episodes shows what was registered."),
)


def _ingest_form(
    error: str = "",
    values: dict[str, str] | None = None,
) -> str:
    """The only way data enters the system, and the first thing a new user sees.

    A plain form POST, so it works with scripting off - consistent with every
    other control here, and the reason it needs no JavaScript to be usable.

    Errors are rendered inline next to the control that caused them rather than
    as a page-level failure. A form that discards what you typed and shows a
    stack trace somewhere else is indistinguishable from the form being broken.
    """
    vals = values or {}
    kind = vals.get("kind", "episode")
    if kind not in dict(_INGEST_KINDS):
        kind = "episode"

    def field(name: str, label: str, placeholder: str, *, wide: bool = False) -> str:
        value = escape(vals.get(name, ""))
        cls = "de-field de-field-wide" if wide else "de-field"
        return (
            f'<label class="{cls}" for="ing-{name}">'
            f'<span class="de-field-label">{escape(label)}</span>'
            f'<input id="ing-{name}" name="{name}" type="text" value="{value}" '
            f'placeholder="{escape(placeholder)}" autocomplete="off" spellcheck="false">'
            "</label>"
        )

    options = "".join(
        f'<option value="{value}"{" selected" if value == kind else ""}>{escape(label)}</option>'
        for value, label in _INGEST_KINDS
    )
    steps = "".join(
        f'<li><span class="de-step-n">{i}</span>{escape(text)}</li>'
        for i, (_, text) in enumerate(_INGEST_STEPS, start=1)
    )
    notice = f'<p class="de-notice de-notice-error">{escape(error)}</p>' if error else ""

    return _section(
        "Ingest data",
        (
            f"{notice}"
            '<ol class="de-steps">' + steps + "</ol>"
            '<form class="de-ingest" method="post" action="/ui/jobs">'
            f'<div class="de-field-row">'
            f'<label class="de-field" for="ing-kind">'
            f'<span class="de-field-label">what are you loading</span>'
            f'<select id="ing-kind" name="kind">{options}</select>'
            "</label></div>"
            f'<div class="de-field-row">'
            + field("task", "task", "pick_place")
            + field("robot", "robot", "arm")
            + field("frames", "frames", "3")
            + "</div>"
            '<div class="de-field-row">'
            + field("source", "dataset path (optional)", "data/lerobot/my_dataset", wide=True)
            + "</div>"
            '<div class="de-field-row">'
            + field(
                "episode_key",
                "which episode (optional)",
                "episode_index=7",
                wide=True,
            )
            + "</div>"
            '<button type="submit" class="de-submit">queue ingest job</button>'
            '<p class="de-sub-note">The worker picks it up within a second. '
            "This is a no-op-safe form: submitting twice creates two jobs.</p>"
            "</form>"
        ),
        "no javascript required",
    )


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


# The clock, the poller and the deadline countdown all live in web/app.js now.
# Keeping them here meant every page carried its own copy of the same three
# functions, and a fix to the failure banner had to be made nine times.


# ---------------------------------------------------------------- status


def status_page(model: dict[str, Any], theme: str) -> str:
    return _page("Status", "/ui", _live("/ui", 3000, _status_body(model)), theme)


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
        f"<tr><td>{_state_badge(str(k))}</td>"
        f'<td class="num">{v}</td>'
        f"<td>{_meter(int(v) / peak)}</td></tr>"
        for k, v in depth.items()
    )
    queue = _section(
        "Queue depth by state",
        _table(
            "Queue depth by job state",
            '<th>State</th><th class="num">Jobs</th><th>Load</th>',
            rows,
        ),
        f"peak {peak}",
    )

    # The form is the *only* way data enters the system, so on a system that has
    # never run it is the most important panel on the page - not an empty-state
    # apology appended below three readouts. It disappears the moment there is
    # anything to look at, because after that it is a distraction from the work.
    #
    # ...except when the last submission was rejected. A failure has to reopen
    # the form even on a populated system, because the error is rendered inside
    # it: gating on emptiness alone means someone who mistypes a field on a busy
    # system gets a 200 with no explanation and no way to correct it.
    onboarding = ""
    ingest_error = str(model.get("ingest_error") or "")
    if ingest_error or (total_jobs == 0 and episodes == 0):
        onboarding = _ingest_form(
            ingest_error,
            model.get("ingest_values") if isinstance(model.get("ingest_values"), dict) else None,
        )

    return readouts + onboarding + queue


# ---------------------------------------------------------------- jobs


def jobs_page(model: dict[str, Any], state_filter: str | None, theme: str) -> str:
    query = f"?state={escape(state_filter)}" if state_filter else ""
    filters = (
        '<form class="de-filters" method="get" action="/ui/jobs">'
        '<label for="state">filter</label>'
        '<select id="state" name="state" class="fine-use-focusable" '
        f'onchange="this.form.submit()">{_options(state_filter)}</select>'
        '<button type="submit" class="fine-use-focusable">apply</button>'
        "</form>"
    )
    body = (
        _section(
            "Queue",
            _live("/ui/jobs" + query, 3000, _jobs_body(model)),
            "newest first",
        )
        + filters
    )
    return _page("Jobs", "/ui/jobs", body, theme)


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
        return _empty(
            "no jobs recorded",
            "queue the first one from the ingest form",
            ("status page", "/ui"),
        )
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/jobs/{escape(str(i["id"]))}">'
        f"{_copyable(str(i['id']), str(i['id'])[:8])}</a></td>"
        f"<td>{escape(str(i['type']))}</td>"
        f"<td>{_state_badge(str(i['state']))}</td>"
        f"<td>{_attempts(i)}</td>"
        f"<td>{_when(i.get('created_at'))}</td>"
        f"<td>{_when(i.get('finished_at'))}</td>"
        f'<td class="dim">{_copyable(str(i["correlation_id"]), str(i["correlation_id"])[:8])}</td>'
        "</tr>"
        for i in items
    )
    return _table(
        "Jobs, newest first",
        "<th>Job</th><th>Type</th><th>State</th><th>Attempts</th>"
        "<th>Queued</th><th>Finished</th><th>Trace</th>",
        rows,
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


def _produced_section(episodes: list[dict[str, Any]] | None) -> str:
    if not episodes:
        return _section("Produced episodes", _empty("this job has not registered episodes"))
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/episodes/{escape(str(e.get("id")))}">'
        f"{_copyable(str(e.get('id')), str(e.get('id'))[:12])}</a></td>"
        f"<td>{escape(str(e.get('episode_key') or '-'))}</td>"
        f"<td>{escape(str(e.get('format') or '-'))}</td>"
        f"<td>{_state_badge(str(e.get('state') or 'ingested'))}</td>"
        f"<td>{_when(e.get('created_at'))}</td>"
        "</tr>"
        for e in episodes
    )
    return _section(
        "Produced episodes",
        _table(
            "Episodes produced by this job",
            "<th>Episode</th><th>Key</th><th>Format</th><th>State</th><th>Registered</th>",
            rows,
        ),
        str(len(episodes)),
    )


def _report_section(report: dict[str, Any] | None) -> str:
    """Triage readout for one run: what it produced, how it validates, how it moves."""
    if not report:
        return ""
    episodes = report.get("episodes") or {}
    validation = report.get("validation") or {}
    by_state = episodes.get("by_state") or {}
    verdicts = episodes.get("verdicts") or {}
    flags = episodes.get("flags") or {}
    codes = validation.get("reason_codes") or {}
    facts = [
        ("episodes", str(int(episodes.get("total", 0)))),
        (
            "states",
            " ".join(
                f"{_state_badge(str(k))} &times; {int(v)}" for k, v in sorted(by_state.items())
            )
            or "-",
        ),
        (
            "verdicts",
            " ".join(
                f"{_verdict_badge(str(k))} &times; {int(v)}" for k, v in sorted(verdicts.items())
            )
            or "not analyzed",
        ),
        (
            "flags",
            f"jerky {int(flags.get('jerky', 0))} // stalled {int(flags.get('stalled', 0))}",
        ),
        (
            "motion",
            f"movement {_score(episodes.get('mean_movement_score'))}"
            f" // jerk {_score(episodes.get('mean_jerk_score'))}"
            f" // stall {_score(episodes.get('mean_stall_ratio'))}",
        ),
        (
            "validation",
            f"{int(validation.get('passed', 0))} passed"
            f" // {int(validation.get('failed', 0))} failed"
            f" over {int(validation.get('results', 0))} results",
        ),
        (
            "reason codes",
            " ".join(
                f"<code>{escape(str(k))} &times; {int(v)}</code>" for k, v in sorted(codes.items())
            )
            or "-",
        ),
    ]
    return _section(
        "Run report",
        '<dl class="de-kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>",
        "one run, end to end",
    )


def job_detail_page(
    job: dict[str, Any],
    theme: str,
    produced: list[dict[str, Any]] | None = None,
    report: dict[str, Any] | None = None,
) -> str:
    state = str(job.get("state"))
    facts = [
        ("job id", _copyable(str(job.get("id")))),
        ("type", escape(str(job.get("type")))),
        ("trace", _copyable(str(job.get("correlation_id")))),
        ("attempts", _attempts(job)),
        ("queued", _when(job.get("created_at"))),
        ("started", _when(job.get("started_at"))),
        ("finished", _when(job.get("finished_at"))),
        ("deadline", _deadline(job)),
    ]
    if job.get("error"):
        facts.append(("error", escape(str((job["error"] or {}).get("code", "see below")))))
    blocks = [
        _section("State", _strip(state) + _cancel_action(job, theme), state),
        _section(
            "Record",
            '<dl class="de-kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>",
        ),
        _section("Payload", f'<pre class="de-json">{escape(_pretty(job.get("payload")))}</pre>'),
    ]
    if job.get("result"):
        blocks.append(
            _section("Result", f'<pre class="de-json">{escape(_pretty(job["result"]))}</pre>')
        )
    if job.get("error"):
        blocks.append(
            _section("Error", f'<pre class="de-json">{escape(_pretty(job["error"]))}</pre>')
        )
    blocks.append(_report_section(report))
    blocks.append(_produced_section(produced))
    episode = (job.get("result") or {}).get("episode_id")
    if episode:
        # A link to the episode's own page, not to a raw document. Handing the
        # operator a JSON blob is a developer affordance wearing an operator's
        # clothes; the page it registers on is the thing they came to look at.
        blocks.append(f'<p><a href="/ui/episodes/{escape(str(episode))}">// open episode</a></p>')
    return _page("Job " + str(job.get("id"))[:8], "/ui/jobs", "".join(blocks), theme)


# ---------------------------------------------------------------- artifacts


def artifacts_page(model: dict[str, Any], theme: str) -> str:
    return _page(
        "Artifacts",
        "/ui/artifacts",
        _section(
            "Stored artifacts",
            _live("/ui/artifacts", 5000, _artifacts_body(model)),
            "content addressed",
        ),
        theme,
    )


def artifacts_fragment(model: dict[str, Any]) -> str:
    return _artifacts_body(model)


def _artifacts_body(model: dict[str, Any]) -> str:
    items = model.get("items", [])
    if not items:
        return _empty("no artifacts stored", "ingest a dataset to produce one")
    total = sum(int(a.get("size_bytes") or 0) for a in items)
    rows = "".join(
        "<tr>"
        f"<td>{_copyable(str(a['hash']), str(a['hash'])[:24])}</td>"
        f'<td class="num">{_bytes(a.get("size_bytes"))}</td>'
        f"<td>{_when(a.get('created_at'))}</td>"
        f'<td class="num">{escape(str(len(a.get("episode_ids") or [])))}</td>'
        "</tr>"
        for a in items
    )
    return f'<p class="de-sub">{len(items)} shown // {_bytes(total)} on this page</p>' + _table(
        "Stored artifacts",
        '<th>Hash</th><th class="num">Size</th><th>Written</th><th class="num">Episodes</th>',
        rows,
    )


# ---------------------------------------------------------------- metrics


def _ms(value: Any) -> str:
    try:
        return f"{float(value) * 1000:.1f}"
    except TypeError, ValueError:
        return "-"


# Traces worth a sparkline: what a data engine's operator watches. Anything not
# present in the window simply does not render.
#: (metric key, operator label, axis unit, drilldown target). The key is an
#: internal name and the label is what the operator reads. The unit goes on the
#: axis because a duration axis with no unit is a number nobody can act on, and
#: the target is where an operator goes once the plot has told them to look.
TRACE_SERIES = (
    ("api_request_duration_seconds", "request handling", "s", "/ui/jobs"),
    ("jobs_run_time_seconds", "job run time", "s", "/ui/jobs"),
    ("pipeline_stage_duration_seconds", "stage duration", "s", "/ui/jobs"),
    ("catalog_query_duration_seconds", "catalog search", "s", "/ui/episodes"),
    ("jobs_queue_depth", "queue depth", "jobs", "/ui/jobs"),
)

#: Plot geometry, mirrored from `web.charts` so the x tick labels land on the
#: same pixels the renderer draws into.
_PLOT_WIDTH = 720


def _series_plot(buckets: list[dict[str, Any]], *, label: str, unit: str, href: str) -> str:
    """One ADR 0017 series as a real plot: axis, unit, and a peak you can follow.

    The previous revision drew a 44-pixel polyline with no axis and no scale, so
    a latency spike and a latency improvement were the same squiggle and neither
    could be located in time. Samples are indexed rather than timestamped because
    the buckets are already ordered and evenly spaced; the x axis carries the
    real clock times, and the caption names the peak with its event count so the
    reader can decide whether it is worth following.
    """
    if not buckets:
        return line_chart([], label=label, caption="no buckets in window")
    values = [float(bucket.get("v") or 0.0) for bucket in buckets]
    peak = max(range(len(values)), key=lambda i: values[i])
    span = max(len(buckets) - 1, 1)
    usable = _PLOT_WIDTH - PAD_LEFT - PAD_RIGHT
    # Clamped, because a single-bucket window has no middle or last index to
    # point at and an out-of-range subscript is how a chart takes down a page.
    x_ticks = [
        (PAD_LEFT + index / span * usable, _bucket_time(buckets[index]))
        for index in sorted({0, span // 2, span})
        if index < len(buckets)
    ]
    caption = (
        f"{len(buckets)} buckets, peak {format_value(values[peak])} {unit} "
        f"at {_bucket_time(buckets[peak])} "
        f"({int(buckets[peak].get('count') or 0)} events), "
        f"mean {format_value(sum(values) / len(values))} {unit}"
    )
    return line_chart(
        list(enumerate(values)),
        label=label,
        unit=unit,
        x_ticks=x_ticks,
        caption=caption,
        link=("drill down", href),
    )


def _bucket_time(bucket: dict[str, Any]) -> str:
    """`2026-09-30T03:38:00+00:00` as `03:38`, falling back to the raw prefix."""
    stamp = str(bucket.get("t", ""))
    match = re.search(r"T([0-9]{2}):([0-9]{2})", stamp)
    return f"{match.group(1)}:{match.group(2)}" if match else stamp[:16]


def metrics_page(model: dict[str, Any], theme: str) -> str:
    return _page("Metrics", "/ui/metrics", _live("/ui/metrics", 5000, _metrics_body(model)), theme)


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
            ("handling p95", _p95(api), "ms"),
            ("run p95", _p95(runs), "ms"),
            ("search p95", _p95(queries), "ms"),
        ]
    )

    traces = "".join(
        _section(
            label,
            _series_plot(series.get(name, []), label=label, unit=unit, href=href),
            f"{len(series.get(name, []))} buckets",
        )
        for name, label, unit, href in TRACE_SERIES
        if series.get(name)
    )
    if not traces:
        traces = _empty("no series in window", "metrics are written as the engine runs")

    # Rows are the operation as a reader would name it. The underlying route
    # stays an internal detail: it is a stable label, but it is a label for the
    # code, not for the person deciding whether this system is healthy.
    api_rows = "".join(
        "<tr>"
        f"<td>{escape(_operation(s['labels'].get('route', '-')))}</td>"
        f"<td>{escape(str(s['labels'].get('method', '-')).upper())}</td>"
        f"<td>{escape(str(s['labels'].get('status_class', '-')))}</td>"
        f'<td class="num">{s["count"]}</td>'
        f'<td class="num">{_ms(s["p50"])}</td>'
        f'<td class="num">{_ms(s["p95"])}</td>'
        f'<td class="num">{_ms(s["p99"])}</td>'
        "</tr>"
        for s in sorted(api, key=lambda s: float(s["p95"]), reverse=True)
    )
    api_table = _section(
        "Time spent handling requests",
        _table(
            "Time spent per operation, slowest first",
            "<th>Operation</th><th>Outcome</th><th>Class</th>"
            '<th class="num">Hits</th><th class="num">p50 ms</th>'
            '<th class="num">p95 ms</th><th class="num">p99 ms</th>',
            api_rows,
            empty="no request traffic in window",
        ),
        "sorted by p95",
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
    tail = _section(
        "Stage + catalog latency",
        _table(
            "Pipeline stage latency",
            '<th>Stage</th><th>Status</th><th class="num">Runs</th>'
            '<th class="num">p50 ms</th><th class="num">p95 ms</th><th>Load</th>',
            stage_rows,
            empty="no stages measured",
        )
        + _table(
            "Catalog operation latency, slowest 20",
            '<th>Catalog op</th><th class="num">Calls</th>'
            '<th class="num">p50 ms</th><th class="num">p95 ms</th>',
            query_rows,
            empty="no catalog queries measured",
        ),
    )
    return readouts + traces + api_table + tail


def _p95(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "-"
    return _ms(max(float(s["p95"]) for s in summaries))


# A route template is a label for the code. An operator reading a latency table
# wants to know which *operation* is slow, and "episode listing" answers that
# where "/api/v1/episodes" only restates the file layout.
#
# The lookup is (path fragment -> operation name) and it is matched against the
# whole template, not just the last segment, because the last segment is often
# the part that varies: "/api/v1/monitoring/tick" ends in "tick". Anything
# unmapped degrades to its last segment with separators tidied, never to the raw
# template, so a newly added route cannot leak its shape into the UI by omission.
_OPERATIONS = (
    ("/health", "health check"),
    ("/monitoring/tick", "monitoring window"),
    ("/monitoring/notify-preview", "notification preview"),
    ("/monitoring", "monitoring"),
    ("/episodes/export", "episode export"),
    ("/quality", "quality scoring"),
    ("/validation", "validation"),
    ("/episodes", "episode listing"),
    ("/artifacts", "artifact listing"),
    ("/incidents/summary", "incident summary"),
    ("/incidents", "incident queue"),
    ("/contracts", "contract checks"),
    ("/slices", "slice listing"),
    ("/metrics", "metrics"),
    ("/failures", "failure listing"),
    ("/status", "status"),
    ("/jobs", "job listing"),
)


def _operation(route: str) -> str:
    """Name the operation a route performs, without disclosing the route."""
    text = str(route or "").strip()
    if not text or text == "-":
        return "unknown"
    lowered = text.lower()
    for fragment, name in _OPERATIONS:
        if fragment in lowered:
            return name
    # Unmapped: the last concrete segment, tidied. No slashes, no braces.
    segments = [seg for seg in lowered.split("/") if seg and not seg.startswith("{")]
    tail = segments[-1] if segments else "unknown"
    return tail.replace("-", " ").replace("_", " ")


# ---------------------------------------------------------------- failures


def failures_page(model: dict[str, Any], theme: str) -> str:
    """What is failing, by reason code, and the episodes it quarantined."""
    return _page(
        "Failures",
        "/ui/failures",
        _live("/ui/failures", 10000, _failures_body(model)),
        theme,
    )


def failures_fragment(model: dict[str, Any]) -> str:
    return _failures_body(model)


def _code_list(codes: Any) -> str:
    """Reason codes as inline chips; a row with none shows a muted dash."""
    values = codes or []
    if not values:
        return '<span class="text-comment">-</span>'
    return " ".join(f"<code>{escape(str(code))}</code>" for code in values)


def _failures_body(model: dict[str, Any]) -> str:
    summary = model.get("summary") or {}
    rows = model.get("items") or []
    codes = summary.get("reason_codes") or {}
    readouts = _readouts(
        [
            ("quarantined", str(int(summary.get("quarantined_count", 0))), "error"),
            ("evaluated", str(int(summary.get("episodes_evaluated", 0))), "info"),
            ("reason codes", str(len(codes)), "warning"),
            ("listed", str(len(rows)), "comment"),
        ]
    )
    code_chips = "".join(
        f'<code class="de-chip text-warning">{escape(str(code))}'
        f"&nbsp;&times;&nbsp;{int(count)}</code>"
        for code, count in sorted(codes.items(), key=lambda kv: -kv[1])
    )
    profile_rows = "".join(
        f"<tr><td><code>{escape(str(p.get('profile_name') or '-'))}</code></td>"
        f'<td class="num">{int(p.get("failed", 0))}</td></tr>'
        for p in (summary.get("by_profile") or [])
    )
    episode_rows = "".join(
        f'<tr><td><a href="/ui/episodes/{escape(str(e.get("id")))}">'
        f"{_copyable(str(e.get('id')), str(e.get('id'))[:12])}</a></td>"
        f"<td>{escape(str(e.get('episode_key') or '-'))}</td>"
        f"<td>{escape(str(e.get('format') or '-'))}</td>"
        f"<td>{_state_badge(str(e.get('state') or 'quarantined'))}</td>"
        f"<td>{escape(str(e.get('profile_name') or '-'))}</td>"
        f"<td>{_code_list(e.get('reason_codes'))}</td>"
        f"<td>{_verdict_cell(e)}</td>"
        "</tr>"
        for e in rows
    )
    return (
        readouts
        + _section(
            "Reason codes",
            code_chips or _empty("nothing quarantined", "validation has rejected nothing"),
            "most frequent first",
        )
        + _section(
            "Failures by profile",
            _table(
                "Failed episodes by validation profile",
                '<th>Profile</th><th class="num">Failed</th>',
                profile_rows,
                empty="no profile has failed",
            ),
        )
        + _section(
            "Quarantined episodes",
            _table(
                "Quarantined episodes",
                "<th>Episode</th><th>Key</th><th>Format</th><th>State</th>"
                "<th>Profile</th><th>Reason codes</th><th>Verdict</th>",
                episode_rows,
                empty="no quarantined episodes",
            ),
            str(len(rows)),
        )
    )


# ---------------------------------------------------------------- incidents


def incidents_page(model: dict[str, Any], theme: str) -> str:
    """The notifier's queue: what it would interrupt you for, and what it would not.

    The notify/queue split is the whole point of the page, so it is stated visually
    rather than buried in a column: an incident that would wake someone is separated
    from one you would simply find tomorrow.
    """
    return _page(
        "Incidents",
        "/ui/incidents",
        _live("/ui/incidents", 10000, _incidents_body(model)),
        theme,
    )


def incidents_fragment(model: dict[str, Any]) -> str:
    return _incidents_body(model)


_SEVERITY_CSS = {
    "critical": "error",
    "high": "warning",
    "medium": "info",
    "info": "comment",
}


def _severity_badge(severity: str) -> str:
    css = _SEVERITY_CSS.get(severity, "comment")
    return f'<span class="text-{css}">{escape(severity)}</span>'


def _incidents_body(model: dict[str, Any]) -> str:
    rows = model.get("items") or []
    summary = model.get("summary") or {}
    health = model.get("health") or {}
    by_severity = summary.get("by_severity") or {}
    open_count = int((summary.get("by_status") or {}).get("open", 0))
    notify_count = int(summary.get("notify_open", 0))

    readouts = _readouts(
        [
            ("open", str(open_count), "error" if open_count else "success"),
            ("would notify", str(notify_count), "warning" if notify_count else "success"),
            ("critical", str(int(by_severity.get("critical", 0))), "error"),
            ("high", str(int(by_severity.get("high", 0))), "warning"),
            ("warm limits", str(int(health.get("warm_scopes", 0))), "info"),
        ]
    )

    health_strip = _health_strip(health)
    digest = model.get("digest") or "No incidents."
    digest_block = f'<pre class="de-digest">{escape(str(digest))}</pre>'

    if not rows:
        table = _empty(
            "the notifier has nothing to say",
            "no window has been evaluated yet",
        )
    else:
        body_rows = "".join(incident_row_html(row) for row in rows)
        table = _table(
            "Incident queue, worst first",
            "<th>Severity</th><th>Label</th><th>Scope</th><th>Evidence</th>"
            '<th class="num">Seen</th><th>Status</th><th>Last seen</th><th>Channel</th>',
            body_rows,
        )

    return (
        readouts
        + health_strip
        + _section(
            "Queue",
            table,
            f"{notify_count} would notify" if notify_count else "nothing needs a human",
        )
        + _section(
            "Notify preview",
            '<p class="de-sub">exactly what a notifier would deliver // '
            "nothing is sent</p>" + digest_block,
            "ADR 0020",
        )
    )


def incident_row_html(row: dict[str, Any]) -> str:
    """One incident row. Public because the escaping is worth testing directly."""
    severity = str(row.get("severity") or "info")
    status = str(row.get("status") or "open")
    channel = str(row.get("notify_class") or "queue")
    notifying = channel == "notify"
    channel_cell = (
        '<span class="text-warning">notify</span>'
        if notifying
        else '<span class="text-comment">queue</span>'
    )
    # A left edge on the row, so the two kinds of incident are separable at a
    # glance without reading the Channel column.
    row_class = ' class="de-notify"' if notifying else ""
    return (
        f"<tr{row_class}>"
        f"<td>{_severity_badge(severity)}</td>"
        f"<td><code>{escape(str(row.get('label') or '-'))}</code></td>"
        f"<td>{escape(str(row.get('scope') or '-'))}</td>"
        f'<td class="de-evidence">{escape(str(row.get("summary") or "-"))}</td>'
        f'<td class="num">{int(row.get("occurrence_count") or 1)}</td>'
        f"<td>{_state_badge(status)}</td>"
        f"<td>{escape(str(row.get('last_seen') or '-'))}</td>"
        f"<td>{channel_cell}</td>"
        "</tr>"
    )


def _health_strip(health: dict[str, Any]) -> str:
    """The monitor's own vitals.

    Shown on the same page as its output on purpose: a monitor that has silently
    stopped looks exactly like a monitor with nothing to report, and only one of
    those is healthy.
    """
    if not health:
        return ""
    last = str(health.get("last_tick_at") or "never")
    blind = "no" if not health.get("blind") else "YES"
    readouts = _readouts(
        [
            ("last tick", escape(last), "info"),
            ("baseline scopes", str(int(health.get("baseline_scopes", 0))), "comment"),
            ("held", str(int(health.get("held_scopes", 0))), "warning"),
            ("budget/window", str(int(health.get("alert_budget_per_window", 0))), "comment"),
            ("blind", blind, "error" if health.get("blind") else "success"),
        ]
    )
    return _section("Monitor health", readouts, "is the notifier itself alive")

    # ---------------------------------------------------------------- slices


def slices_page(model: dict[str, Any], theme: str) -> str:
    """Named curation filters and a link to each slice's manifest."""
    return _page(
        "Slices",
        "/ui/slices",
        _section(
            "Saved slices",
            _live("/ui/slices", 10000, _slices_body(model)),
            "membership recomputed on read",
        ),
        theme,
    )


def slices_fragment(model: dict[str, Any]) -> str:
    return _slices_body(model)


def _slices_body(model: dict[str, Any]) -> str:
    items = model.get("items") or []
    if not items:
        return _empty("no saved slices", "save a filter from Episodes to create one")
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/slices/{escape(str(s.get("id")))}"><code>'
        f"{escape(str(s.get('name')))}</code></a></td>"
        f'<td class="num">{int(s.get("member_count", 0))}</td>'
        f"<td>{escape(str(s.get('notes') or '-'))}</td>"
        f"<td><code>{escape(str((s.get('filter_config') or {}).get('state') or 'any'))}"
        f" / {escape(str((s.get('filter_config') or {}).get('flag') or 'any'))}</code></td>"
        f"<td>{_when(s.get('updated_at'))}</td>"
        # A manifest is the build input: a machine-readable list of the exact
        # episodes this slice resolves to. Downloading one is a legitimate
        # operator action, so the link stays - but it is a download of a named
        # artifact, not a hand-off to a raw endpoint path.
        f'<td><a href="/api/v1/slices/{escape(str(s.get("id")))}/manifest" download>'
        "// download manifest</a></td>"
        "</tr>"
        for s in items
    )
    return _table(
        "Saved curation slices",
        '<th>Name</th><th class="num">Members</th><th>Notes</th>'
        "<th>Filter</th><th>Updated</th><th>Manifest</th>",
        rows,
    )


def slice_impact_page(impact: dict[str, Any], theme: str) -> str:
    """What this slice drops versus the whole dataset, and why.

    The why is grounded in the quality signals the filter actually tests -
    verdict, stall ratio, state - and each side carries its own medians, so
    "this slice is smoother than what it dropped" is readable off the page
    instead of inferred from a member count.
    """
    filters = impact.get("filters") or {}
    kept = impact.get("kept") or {}
    dropped = impact.get("dropped") or {}
    dataset = impact.get("dataset") or {}
    total = int(dataset.get("episodes") or 0)
    kept_n = int(kept.get("count") or 0)
    dropped_n = int(dropped.get("count") or 0)
    share = kept_n / total if total else 0.0

    facts = [
        ("slice", escape(str(impact.get("name") or impact.get("slice_id")))),
        (
            "filter",
            f"<code>{escape(str(filters.get('state') or 'any'))}"
            f" / {escape(str(filters.get('flag') or 'any'))}</code>",
        ),
    ]
    readouts = _readouts(
        [
            ("dataset", str(total), "episodes"),
            ("kept", f'<span class="text-success">{kept_n}</span>', f"{share * 100:.0f}%"),
            (
                "dropped",
                f'<span class="text-warning">{dropped_n}</span>',
                f"{(1 - share) * 100:.0f}%",
            ),
            ("scored kept", str(int(kept.get("scored") or 0)), "of " + str(kept_n)),
            ("scored dropped", str(int(dropped.get("scored") or 0)), "of " + str(dropped_n)),
        ]
    )
    meter = _section("Keep rate", _meter(_bounded(share), 32), f"{kept_n} of {total}")

    reasons = impact.get("drop_reasons") or []
    if impact.get("reorders_only"):
        why = _empty(
            "this filter reorders the view; it excludes nothing",
            "short/long are orderings, so nothing is dropped and nothing can be",
        )
    elif not reasons:
        why = _empty("nothing was dropped", "every episode in the catalog is a member")
    else:
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(str(r.get('reason')))}</code></td>"
            f'<td class="num">{int(r.get("count") or 0)}</td>'
            "<td>"
            + _meter(_bounded((int(r.get("count") or 0)) / dropped_n if dropped_n else 0.0), 12)
            + "</td>"
            "</tr>"
            for r in reasons
        )
        why = _table(
            "Why episodes dropped",
            '<th>Reason</th><th class="num">Episodes</th><th>Share</th>',
            rows,
        )

    def cell(side: dict[str, Any], key: str) -> str:
        value = side.get(key)
        return "-" if value is None else _score(value)

    comparison = _table(
        "Kept versus dropped, on the quality signals the filter tests",
        "<th>Signal</th><th>Kept</th><th>Dropped</th>",
        "".join(
            f"<tr><td>{label}</td><td>{cell(kept, key)}</td><td>{cell(dropped, key)}</td></tr>"
            for label, key in (
                ("median jerk score", "median_jerk_score"),
                ("median stall ratio", "median_stall_ratio"),
                ("median frames", "median_frames"),
                ("gapped recordings", "gapped"),
                ("scored episodes", "scored"),
            )
        ),
    )
    body = (
        '<dl class="de-kv">'
        + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
        + "</dl>"
        + readouts
        + meter
        + _section("What it drops", why, "grounded in the filter's own predicates")
        + _section("What that costs", comparison, "kept vs dropped")
        + _section(
            "Manifest",
            f'<a href="/api/v1/slices/{escape(str(impact.get("slice_id")))}/manifest" download>'
            '// download manifest</a> &nbsp; <a href="/ui/slices">// all slices</a>',
            "build input",
        )
    )
    return _page("Slice " + str(impact.get("name") or "")[:24], "/ui/slices", body, theme)


# ---------------------------------------------------------------- episodes


def episodes_page(
    model: dict[str, Any], state_filter: str | None, flag: str | None, theme: str
) -> str:
    query = ""
    if state_filter or flag:
        pairs = [f"state={escape(state_filter or '')}", f"flag={escape(flag or '')}"]
        query = "?" + "&".join(pairs)
    filters = (
        '<form class="de-filters" method="get" action="/ui/episodes">'
        '<label for="state">state</label>'
        '<select id="state" name="state" class="fine-use-focusable" '
        f'onchange="this.form.submit()">{_pick(EPISODE_STATES, state_filter)}</select>'
        '<label for="flag">flag</label>'
        '<select id="flag" name="flag" class="fine-use-focusable" '
        f'onchange="this.form.submit()">{_pick(EPISODE_FLAGS, flag, empty="all")}</select>'
        '<button type="submit" class="fine-use-focusable">apply</button>'
        "</form>"
    )
    body = (
        _section(
            "Episodes",
            _live("/ui/episodes" + query, 5000, _episodes_body(model)),
            "quality scored at ingest",
        )
        + filters
    )
    return _page("Episodes", "/ui/episodes", body, theme)


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
        return _empty("no episodes match", "clear the filters to see everything")
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/episodes/{escape(str(i["id"]))}">'
        f"{_copyable(str(i['id']), str(i['id'])[:8])}</a></td>"
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
    return _table(
        "Episodes matching the current filter",
        "<th>Episode</th><th>Key</th><th>Format</th><th>State</th>"
        '<th class="num">Frames</th><th class="num">Move</th><th class="num">Jerk</th>'
        "<th>Stall</th><th>Verdict</th><th>Ingested</th>",
        rows,
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


def _validation_section(validations: list[dict[str, Any]] | None) -> str:
    if not validations:
        return _section("Validation", _empty("no validation runs for this episode"))

    def _verdict(v: dict[str, Any]) -> str:
        if v.get("passed"):
            return '<span class="text-success">PASS</span>'
        return '<span class="text-error">QUARANTINED</span>'

    rows = "".join(
        "<tr>"
        f"<td><code>{escape(str(v.get('profile_name') or '-'))}</code>"
        f'<span class="text-comment">@{escape(str(v.get("profile_version") or "-"))}</span></td>'
        f"<td>{_copyable(_text(v.get('profile_hash')), _text(v.get('profile_hash'))[:12])}</td>"
        f"<td>{_verdict(v)}</td>"
        f"<td>{escape(', '.join(str(c) for c in (v.get('reason_codes') or [])) or '-')}</td>"
        "</tr>"
        for v in validations
    )
    violation_blocks = []
    for v in validations:
        for violation in v.get("violations") or []:
            violation_blocks.append(
                "<li>"
                f"<code>{escape(str(violation.get('code') or '-'))}</code> "
                f"{escape(str(violation.get('message') or ''))}"
                f'<span class="text-comment"> [{escape(str(v.get("profile_name") or "-"))}]</span>'
                "</li>"
            )
    violations_html = (
        '<h3 class="de-sub">Violations</h3><ul class="de-violations">'
        + "".join(violation_blocks)
        + "</ul>"
        if violation_blocks
        else '<p class="de-empty">// no violations</p>'
    )
    passed = sum(1 for v in validations if v.get("passed"))
    return _section(
        "Validation",
        _table(
            "Validation results by profile",
            "<th>Profile</th><th>Hash</th><th>Verdict</th><th>Reasons</th>",
            rows,
        )
        + violations_html,
        f"{passed}/{len(validations)} passed",
    )


def episode_detail_page(
    episode: dict[str, Any],
    quality: dict[str, Any] | None,
    theme: str,
    validations: list[dict[str, Any]] | None = None,
) -> str:
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
        _section(
            "Record",
            '<dl class="de-kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>",
        )
    ]
    blocks.append(_quality_section(quality))
    blocks.append(_validation_section(validations))
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
            _section(
                "Channels",
                _table(
                    "Per-channel statistics",
                    '<th>Channel</th><th class="num">n</th><th class="num">min</th>'
                    '<th class="num">max</th><th class="num">mean</th>'
                    '<th class="num">std</th>',
                    rows,
                ),
                str(len(channels)),
            )
        )
    blocks.append(
        _section("Metadata", f'<pre class="de-json">{escape(_pretty(meta))}</pre>', "raw")
    )
    return _page("Episode " + str(episode.get("id"))[:8], "/ui/episodes", "".join(blocks), theme)


def _quality_section(quality: dict[str, Any] | None) -> str:
    if not quality:
        return _section("Motion quality", _empty("no quality signals for this episode"))
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
        table = _table(
            "Per-dimension motion statistics",
            "<th>Dim</th><th>Mode</th><th>Kind</th><th>Grip</th>"
            '<th>sigma(d)/range</th><th class="num">mean|d|/range</th>',
            rows,
        )
    return _section("Motion quality", readouts + _motion_trace(quality) + table, "ADR 0018")


def _motion_trace(quality: dict[str, Any]) -> str:
    """The motion score over time, with recording gaps drawn as holes.

    Runs come from `analyze` already bounded and already split at dropouts; the
    only work here is to put a `None` break between them so `line_chart` draws
    separate segments. Nothing reconnects across a hole, and the hole keeps its
    measured width because x is mapped by value, not by index.
    """
    runs = quality.get("motion_trace") or []
    points = [(float(t), float(v)) for run in runs for t, v in run]
    if not points:
        return ""
    samples: list[tuple[float, float | None]] = []
    for run in runs:
        if samples and run:
            samples.append((float(run[0][0]), None))
        samples.extend((float(t), float(v)) for t, v in run)
    lo = min(t for t, _ in points)
    hi = max(t for t, _ in points)
    usable = _PLOT_WIDTH - PAD_LEFT - PAD_RIGHT

    def tick(t: float) -> float:
        share = (t - lo) / (hi - lo) if hi > lo else 0.0
        return PAD_LEFT + share * usable

    ticks = [(tick(lo), f"{lo:.0f} s"), (tick(hi), f"{hi:.0f} s")]
    if hi - lo > 4:
        ticks.insert(1, (tick((lo + hi) / 2), f"{(lo + hi) / 2:.0f} s"))
    caption = f"{len(points)} points in {len(runs)} run(s)"
    gaps = len(runs) - 1
    if gaps:
        noun = "gap" if gaps == 1 else "gaps"
        caption += f"; the {gaps} {noun} between runs are recording dropouts, not stillness"
    else:
        caption += "; continuous recording"
    return line_chart(
        samples,
        label="motion over time",
        unit="mean |d|/range",
        x_ticks=ticks,
        caption=caption,
    )


# ---------------------------------------------------------------- insights


def insights_page(model: dict[str, Any], theme: str) -> str:
    return _page(
        "Insights",
        "/ui/insights",
        _live("/ui/insights", 10000, _insights_body(model)),
        theme,
    )


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

    length_section = _section(
        "Episode lengths",
        _histogram_svg(length.get("histogram") or [])
        + f'<p class="de-sub">min {escape(str(length.get("min", "-")))}'
        f" // max {escape(str(length.get('max', '-')))} frames</p>",
        f"mean {_score(length.get('mean'))}",
    )

    # Plotted on `jerk_score`, not `movement_score`. The raw score is an L2 norm
    # in the source's own units, so the driving fixture (millimetres) reads
    # 1.0e8 beside arm joints in radians at 0.02 and every arm episode collapses
    # onto the floor of the axis. `jerk_score` is the same motion divided by
    # each dimension's range, so the five episodes of the reference run land in
    # a 4x band instead of a 4-billion-x one and the chart can be read. The raw
    # score is still in the table below, labelled with its unit.
    speed = model.get("speed_distribution", [])
    speed_section = _section(
        "Motion, comparable across datasets",
        (
            _episode_dots(speed, "jerk_score")
            if speed
            else _empty("no scored episodes", "ingest something to populate this")
        )
        + '<p class="de-sub">mean |delta| / range, per frame &#183; dimensionless'
        " &#183; higher is faster relative to the joint's own travel</p>"
        + _table(
            "Per-episode motion in both units",
            "<th>Episode</th>"
            '<th class="num">normalised</th><th class="num">raw /frame</th>'
            "<th>Integrity</th><th>Verdict</th>",
            "".join(
                "<tr>"
                f'<td><a href="/ui/episodes/{escape(str(row.get("episode_id")))}">'
                f"{_copyable(str(row.get('episode_id')), str(row.get('episode_id'))[:8])}</a></td>"
                f'<td class="num">{_score(row.get("jerk_score"))}</td>'
                f'<td class="num">{_score(row.get("movement_score"))}</td>'
                f"<td>{_integrity_badge(row.get('integrity'))}</td>"
                f"<td>{_verdict_badge(row.get('verdict'))}</td>"
                "</tr>"
                for row in speed
            ),
            empty="no scored episodes",
        ),
        "one row per episode",
    )

    # Recording reliability, rolled up. Nothing in the pipeline counts this: a
    # dataset assembled half from gappy recordings is a dataset with holes, and
    # per-episode verdicts are not a number anyone can hold in their head.
    integrity_counts = model.get("integrity") or {}
    gapped = int(integrity_counts.get("gapped", 0))
    scored = sum(int(v) for v in integrity_counts.values())
    reliability = _section(
        "Recording reliability",
        bar_chart(
            [(name, count) for name, count in sorted(integrity_counts.items())],
            label="episodes by temporal integrity",
            unit="episodes",
            highlight="gapped" if gapped else None,
        )
        + f'<p class="de-sub">{gapped} of {scored} recordings dropped frames'
        " &#183; a gapped episode has a hole in the middle of it that no later"
        " stage can detect</p>",
        "ADR 0023",
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
                f"{_copyable(str(e['episode_id']), str(e['episode_id'])[:8])}</a></td>"
                f"{cells}</tr>"
            )
        heat = _section(
            "Cross-episode variance // sigma(d)/range",
            '<div class="de-heat-wrap" tabindex="0" role="region" '
            'aria-label="Per-dimension variance, scrollable">'
            '<table class="de-table de-heat-table">'
            '<caption class="de-sr">Per-dimension normalised standard deviation, '
            "brighter cells are rougher</caption>"
            f"<thead><tr><th>Ep</th>{head}</tr></thead><tbody>{body_rows}</tbody></table></div>"
            '<p class="de-sub">brighter = rougher // hover for values</p>',
            f"{len(dims)} dims",
        )

    outliers = model.get("outliers", {})
    lists = "".join(
        '<div><h3 class="de-outlier-h">'
        + escape(title)
        + '</h3><ul class="de-outliers">'
        + "".join(
            f'<li><a href="/ui/episodes/{escape(str(o.get("episode_id")))}">'
            f"{_copyable(str(o.get('episode_id')), str(o.get('episode_id'))[:8])}</a> "
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
    outlier_section = _section(
        "Outliers // removal candidates",
        (
            f'<div class="de-outlier-grid">{lists}</div>'
            if lists
            else _empty("no data", "score more episodes to populate this")
        ),
    )
    return readouts + length_section + speed_section + reliability + heat + outlier_section
