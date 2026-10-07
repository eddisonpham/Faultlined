"""Server-rendered SVG chart primitives (ADR 0024)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

__all__ = [
    "DECADES_BEFORE_LOG",
    "Scale",
    "axis_ticks",
    "bar_chart",
    "choose_scale",
    "format_value",
    "line_chart",
    "polyline_points",
]

DECADES_BEFORE_LOG = 2.0

PAD_LEFT = 52
PAD_RIGHT = 10
PAD_TOP = 10
PAD_BOTTOM = 22

FONT = 10.0


def _anchor(x: float, left: float, right: float) -> str:
    """Which end a tick label should hang from, given where it sits."""
    if x <= left + 1.0:
        return "start"
    if x >= right - 1.0:
        return "end"
    return "middle"


def _plural(count: int, noun: str) -> str:
    """`1 bucket` / `4 buckets`."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def format_value(value: float) -> str:
    """Short, readable, and honest about magnitude."""
    if value == 0:
        return "0"
    if not math.isfinite(value):
        return "inf" if value > 0 else "-inf"
    magnitude = abs(value)
    if magnitude < 0.001 or magnitude >= 1e6:
        return f"{value:.1e}"
    if value == int(value):
        return str(int(value))
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


@dataclass(frozen=True, slots=True)
class Scale:
    """A value-to-pixel mapping, linear or logarithmic, over a known domain."""

    lo: float
    hi: float
    logarithmic: bool = False

    def pixel(self, value: float, low: float, high: float) -> float:
        """Map `value` into the pixel band `[low, high]`, `high` being the top."""
        if not math.isfinite(value):
            return low
        if self.logarithmic:
            safe = max(value, self.lo)
            span = math.log10(self.hi) - math.log10(self.lo)
            if span <= 0:
                return low
            fraction = (math.log10(safe) - math.log10(self.lo)) / span
        else:
            span = self.hi - self.lo
            fraction = 0.0 if span <= 0 else (value - self.lo) / span
        fraction = min(max(fraction, 0.0), 1.0)
        return low - fraction * (low - high)


def choose_scale(values: Sequence[float], *, decades: float = DECADES_BEFORE_LOG) -> Scale:
    """Pick a linear or log10 domain for `values`, and say which in the result."""
    finite = [float(v) for v in values if math.isfinite(float(v))]
    if not finite:
        return Scale(0.0, 1.0, False)
    lo, hi = min(finite), max(finite)
    positive = [v for v in finite if v > 0]
    loggable = lo > 0 and len(positive) == len(finite) and math.log10(hi / lo) > decades
    if loggable:
        return Scale(lo, hi, True)
    if lo == hi:
        pad = abs(lo) * 0.5 or 1.0
        return Scale(lo - pad, hi + pad, False)
    return Scale(lo, hi, False)


def polyline_points(
    samples: Sequence[tuple[float, float | None]],
    scale: Scale,
    *,
    x0: float,
    x1: float,
    top: float,
    bottom: float,
) -> list[list[tuple[float, float]]]:
    """Split `samples` into contiguous runs, one per list, breaking on `None`."""
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    span = x1 - x0 or 1.0
    drawn = [
        float(x_value)
        for x_value, y_value in samples
        if y_value is not None and math.isfinite(y_value)
    ]
    lo, hi = (min(drawn), max(drawn)) if drawn else (0.0, 1.0)
    for x_value, y_value in samples:
        if y_value is None or not math.isfinite(y_value):
            if current:
                runs.append(current)
                current = []
            continue
        share = (float(x_value) - lo) / (hi - lo) if hi > lo else 0.0
        x = x0 + share * span
        current.append((x, scale.pixel(y_value, bottom, top)))
    if current:
        runs.append(current)
    return runs


def axis_ticks(scale: Scale, count: int = 4) -> list[float]:
    """`count` round tick values spanning `scale`'s domain, including both ends."""
    lo, hi, logarithmic = scale.lo, scale.hi, scale.logarithmic
    if logarithmic:
        low_exp, high_exp = math.log10(lo), math.log10(hi)
        exponent_step = max(1, math.ceil((high_exp - low_exp) / max(count - 1, 1)))
        return [10.0**e for e in range(math.floor(low_exp), math.ceil(high_exp) + 1, exponent_step)]
    value_step = (hi - lo) / max(count - 1, 1)
    return [lo + value_step * i for i in range(count)]


def _frame(width: int, height: int, label: str, css: str) -> str:
    return (
        f'<svg class="de-plot {css}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}" preserveAspectRatio="xMidYMid meet">'
    )


def _y_axis(scale: Scale, width: int, top: float, bottom: float, count: int = 4) -> str:
    out = []
    for value in axis_ticks(scale, count):
        y = scale.pixel(value, bottom, top)
        if not (top - 1 <= y <= bottom + 1):
            continue
        out.append(
            f'<line class="de-grid" x1="{PAD_LEFT}" y1="{y:.1f}" '
            f'x2="{width - PAD_RIGHT}" y2="{y:.1f}"/>'
            f'<text class="de-tick" x="{PAD_LEFT - 6}" y="{y + 3:.1f}" '
            f'text-anchor="end">{escape(format_value(value))}</text>'
        )
    return "".join(out)


def line_chart(
    samples: Sequence[tuple[float, float | None]],
    *,
    width: int = 720,
    height: int = 180,
    label: str = "series",
    unit: str = "",
    x_ticks: Sequence[tuple[float, str]] = (),
    caption: str = "",
    link: tuple[str, str] | None = None,
) -> str:
    """A traced series with a real y axis, a real x axis, and visible gaps."""
    if not samples:
        return _empty_plot(label, caption)

    values = [v for _, v in samples if v is not None and math.isfinite(v)]
    scale = choose_scale(values)
    top, bottom = PAD_TOP, height - PAD_BOTTOM

    runs = polyline_points(
        samples, scale, x0=PAD_LEFT, x1=width - PAD_RIGHT, top=top, bottom=bottom
    )
    traces = "".join(
        '<polyline class="de-trace" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in run) + '"/>'
        for run in runs
        if len(run) > 1
    )
    markers = "".join(
        f'<circle class="de-point" cx="{x:.1f}" cy="{y:.1f}" r="2"/>'
        for run in runs
        for x, y in run
    )
    left, right = PAD_LEFT, width - PAD_RIGHT
    x_axis = "".join(
        f'<text class="de-tick" x="{x:.1f}" y="{bottom + 14:.1f}" '
        f'text-anchor="{_anchor(x, left, right)}">{escape(text)}</text>'
        for x, text in x_ticks
    )

    described = (
        f"{label}: {_plural(len(values), 'sample')}, "
        f"{format_value(scale.lo)} to {format_value(scale.hi)}"
        f"{' log10' if scale.logarithmic else ''}"
        f"{' ' + unit if unit else ''}"
    )
    note = _note(caption, link)
    return (
        _frame(width, height, described, "de-plot-line")
        + _y_axis(scale, width, top, bottom)
        + x_axis
        + traces
        + markers
        + "</svg>"
        + note
    )


def bar_chart(
    buckets: Sequence[tuple[str, float]],
    *,
    width: int = 720,
    height: int = 180,
    label: str = "distribution",
    unit: str = "",
    highlight: str | None = None,
) -> str:
    """Labelled bars."""
    if not buckets:
        return _empty_plot(label, "")
    values = [float(v) for _, v in buckets]
    scale = choose_scale(values, decades=math.inf)
    top, bottom = PAD_TOP, height - PAD_BOTTOM
    slot = (width - PAD_LEFT - PAD_RIGHT) / len(buckets)
    baseline = scale.pixel(max(scale.lo, 0.0), bottom, top)

    bars = []
    for index, (name, value) in enumerate(buckets):
        x = PAD_LEFT + index * slot
        y = scale.pixel(float(value), bottom, top)
        bar_top, bar_height = min(y, baseline), abs(baseline - y)
        css = "de-bar de-bar-hi" if highlight is not None and name == highlight else "de-bar"
        bars.append(
            f'<rect class="{css}" x="{x + 1:.1f}" y="{bar_top:.1f}" '
            f'width="{max(slot - 2, 1):.1f}" height="{max(bar_height, 0.5):.1f}">'
            f"<title>{escape(name)}: {escape(format_value(value))}</title></rect>"
        )
    labels = "".join(
        f'<text class="de-tick" x="{PAD_LEFT + index * slot + slot / 2:.1f}" '
        f'y="{bottom + 14:.1f}" text-anchor="middle">{escape(name)}</text>'
        for index, (name, _) in enumerate(buckets)
        if len(buckets) <= 16 or index % 2 == 0
    )
    described = (
        f"{label}: {_plural(len(buckets), 'bucket')}, peak {format_value(max(values))}"
        f"{' ' + unit if unit else ''}"
    )
    return (
        _frame(width, height, described, "de-plot-bars")
        + _y_axis(scale, width, top, bottom, count=3)
        + "".join(bars)
        + labels
        + "</svg>"
    )


def _note(caption: str, link: tuple[str, str] | None) -> str:
    """The text under a chart: escaped prose, plus an optional real link."""
    if not caption and link is None:
        return ""
    parts = [escape(caption)] if caption else []
    if link is not None:
        label, href = link
        parts.append(f'<a href="{escape(href)}">{escape(label)}</a>')
    return f'<p class="de-chart-note">{" &#183; ".join(parts)}</p>'


def _empty_plot(label: str, caption: str) -> str:
    """An explicit empty state."""
    return (
        _frame(720, 120, f"{label}: no data", "de-plot de-plot-empty")
        + '<text class="de-tick" x="360" y="64" text-anchor="middle">no data</text>'
        + "</svg>"
        + _note(caption, None)
    )
