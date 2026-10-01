"""Three views of a clustering, as static SVG.

The pair metrics say how well a clustering scores. They do not say whether it looks
like anything a person would recognise, and EXP-2.5-04 is the reason: a pair F1 of
0.94 is reported identically whether the merges are right or wrong, which is why the
boundary audit had to dump individual strings before the answer could be trusted.
These figures are the same idea one level up — the shape of the whole clustering,
rather than the strings nearest one boundary.

**Static SVG, no chart library.** Nothing here imports matplotlib or d3. The engine
has no frontend build, the UI is server-rendered (ADR 0014), and a reviewable
artefact that lives in `results/` next to the JSON it was generated from is worth
more than an interactive page that cannot be diffed. Three hand-written renderers
and a squarified treemap are a few hundred lines; the dependency they would replace
would be considerably more.

**The three views answer three different questions.**

*Treemap* — how is the corpus split? Area is cluster size, so one glance shows
whether the clustering has produced four tasks or four hundred singletons, which no
scalar metric makes obvious.

*2D map with hulls* — do the clusters occupy separate territory? The projection is
PCA, which is the *worst* possible way to make clusters look separated, since it
ignores the metric the clustering actually used. Points that overlap after PCA were
almost certainly not separable at any radius either, so this view is biased against
the method rather than for it.

*Heatmap* — what is inside each cluster? This is the one that catches a clustering
which scores well for the wrong reason, which is exactly the failure EXP-2.5-05
found when verb synonyms split despite naming the same object.

Colours in the 2D map are derived from the cluster id by a hash, so they are stable
between runs and carry no meaning; the figure is not encoding a score in its palette.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

#: Canvas margins shared by every figure, so a set of three lines up visually.
_MARGIN = 24
_LABEL_SIZE = 11


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle in data space, before it becomes an SVG element."""

    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height


def _escape(text: str) -> str:
    return html.escape(str(text), quote=True)


def _svg(width: int, height: int, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_escape(title)}">'
        f"<title>{_escape(title)}</title>"
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f"{body}</svg>\n"
    )


def _text(
    x: float, y: float, content: str, *, size: int = _LABEL_SIZE, fill: str = "#1f2933"
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="ui-monospace, monospace" '
        f'font-size="{size}" fill="{fill}">{_escape(content)}</text>'
    )


def _cluster_hue(index: int) -> str:
    """A stable colour per cluster. Decorative, so a hash is enough."""
    golden = 0.9 + 0.7 * ((index * 0.618033988749895) % 1.0)
    red, green, blue = _hsv_to_rgb(golden * 0.22, 0.45, 0.86)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _hsv_to_rgb(hue: float, saturation: float, value: float) -> tuple[int, int, int]:
    hue = hue % 1.0
    sector = hue * 6.0
    fraction = sector - int(sector)
    p = value * (1.0 - saturation)
    q = value * (1.0 - saturation * fraction)
    t = value * (1.0 - saturation * (1.0 - fraction))
    red, green, blue = [
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    ][int(sector) % 6]
    return round(red * 255), round(green * 255), round(blue * 255)


# ------------------------------------------------------------------ treemap


def squarify(sizes: Sequence[float], width: float, height: float) -> list[Rect]:
    """Lay rectangles out by area with roughly square aspect ratios.

    Plain slice-and-dice alternates the cutting direction, which is three lines of
    code and produces long thin slivers for unequal areas — a 200-episode cluster
    next to a 2-episode one becomes an unreadable stripe. Squarified layout keeps
    the aspect ratio near 1 by growing each row along its shorter side.

    Rectangles come back in the order the sizes were given. Matching them back up by
    area afterwards looks simpler and is wrong: two clusters of the same size are
    indistinguishable, so `list.index` would label half the figure with the first
    cluster's name.
    """
    if not sizes:
        return []
    total = float(sum(sizes))
    if total <= 0:
        raise ValueError("treemap areas must sum to a positive value")
    scale = width * height / total
    placed: dict[int, Rect] = {}
    items: list[tuple[float, int]] = []
    x = y = 0.0
    w, h = width, height
    row: list[tuple[float, int]] = []

    def worst(row: list[tuple[float, int]], side: float) -> float:
        """Worst aspect ratio in a candidate row; lower is squarer."""
        areas = [item[0] for item in row]
        length = sum(areas)
        if length <= 0:
            return float("inf")
        thickness = length / side
        worst_ratio = 0.0
        for area in areas:
            extent = area / thickness
            worst_ratio = max(worst_ratio, thickness / extent, extent / thickness)
        return worst_ratio

    def flush() -> None:
        nonlocal row, x, y, w, h
        if not row:
            return
        areas = [item[0] for item in row]
        # Each row is laid along the **shorter** remaining side, so the row's full
        # extent is exactly that side and the layout cannot run off the canvas.
        # Laying along the longer side overflows by construction, and the overflow
        # is invisible in the numbers - the areas still sum correctly - so the
        # figures carried it until a containment test caught it.
        if w >= h:
            thickness = sum(areas) / h
            offset = 0.0
            for area, index in row:
                extent = area / thickness
                placed[index] = Rect(x, y + offset, thickness, extent)
                offset += extent
            x += thickness
            w -= thickness
        else:
            thickness = sum(areas) / w
            offset = 0.0
            for area, index in row:
                extent = area / thickness
                placed[index] = Rect(x + offset, y, extent, thickness)
                offset += extent
            y += thickness
            h -= thickness
        row = []

    for index, size in enumerate(sizes):
        area = float(size) * scale
        if area <= 0.0:
            # Kept in the output so there is exactly one rectangle per input, in
            # order. Dropping it makes the caller's `strict=True` zip raise, and an
            # empty cluster is a real thing a clustering can produce.
            placed[index] = Rect(x, y, 0.0, 0.0)
            continue
        item = (area, index)
        items.append(item)
        candidate = [*row, item]
        side = w if w >= h else h
        if row and worst(candidate, side) > worst(row, side):
            flush()
        row.append(item)
    flush()
    return [placed[index] for index in range(len(sizes))]


def render_treemap(
    sizes: Sequence[int],
    labels: Sequence[str],
    title: str,
    width: int = 720,
    height: int = 440,
) -> str:
    """Cluster sizes as area, each labelled with its count."""
    if len(sizes) != len(labels):
        raise ValueError("treemap needs one label per cluster")
    rects = squarify([float(size) for size in sizes], width - 2 * _MARGIN, height - 2 * _MARGIN)
    parts: list[str] = []
    for index, (rect, size, label) in enumerate(zip(rects, sizes, labels, strict=True)):
        parts.append(
            f'<rect x="{rect.x + _MARGIN:.2f}" y="{rect.y + _MARGIN:.2f}" '
            f'width="{max(rect.width, 0.5):.2f}" height="{max(rect.height, 0.5):.2f}" '
            f'fill="{_cluster_hue(index)}" fill-opacity="0.55" '
            f'stroke="#ffffff" stroke-width="1"/>'
        )
        if rect.width > 34 and rect.height > 13:
            parts.append(
                _text(
                    rect.x + _MARGIN + 4,
                    rect.y + _MARGIN + 12,
                    f"{label} n={size}",
                    fill="#111827",
                )
            )
    parts.append(_text(_MARGIN, _MARGIN - 8, f"{title}  ({len(sizes)} clusters)"))
    return _svg(width, height, "".join(parts), title)


# ---------------------------------------------------------------- 2D + hull


def convex_hull(points: np.ndarray) -> np.ndarray:
    """Andrew's monotone chain hull, counter-clockwise.

    Collinear points are dropped from the hull edges but kept in the scatter, which
    is the point of drawing both: the hull says what the cluster encloses and the
    points say what is actually in it.
    """
    if len(points) < 3:
        return points
    ordered = points[np.lexsort((points[:, 1], points[:, 0]))]

    def cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list[np.ndarray] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[np.ndarray] = []
    for point in ordered[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return np.array(lower[:-1] + upper[:-1])


def project_2d(matrix: np.ndarray) -> tuple[np.ndarray, tuple[float, float]]:
    """First two principal components, and the share of variance each explains.

    PCA by SVD rather than a library call, and deliberately: this projection knows
    nothing about the cosine metric the clustering used, which is what makes the map
    an honest worst case rather than a flattering picture.
    """
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        raise ValueError("need at least two vectors to project")
    centred = matrix - matrix.mean(axis=0, keepdims=True)
    _left, singular, right = np.linalg.svd(centred, full_matrices=False)
    variance = singular**2
    total = float(variance.sum()) or 1.0
    coords = centred @ right[:2].T
    return coords, (float(variance[0] / total), float(variance[1] / total))


def render_map(
    vectors: np.ndarray,
    labels: Sequence[int],
    title: str,
    width: int = 720,
    height: int = 520,
) -> str:
    """PCA scatter with a convex hull around every cluster of three or more."""
    coords, explained = project_2d(vectors)
    plot_w, plot_h = width - 2 * _MARGIN, height - 2 * _MARGIN - 18
    low = coords.min(axis=0)
    span = coords.max(axis=0) - low
    span[span == 0.0] = 1.0

    def to_canvas(point: np.ndarray) -> tuple[float, float]:
        x = (point[0] - low[0]) / span[0] * (plot_w - 20) + 10
        y = plot_h - ((point[1] - low[1]) / span[1] * (plot_h - 20) + 10)
        return x + _MARGIN, y + _MARGIN

    parts: list[str] = []
    groups: dict[int, list[np.ndarray]] = {}
    for point, label in zip(coords, labels, strict=True):
        groups.setdefault(int(label), []).append(point)

    for label in sorted(groups):
        members = np.array(groups[label])
        hull = convex_hull(members)
        if len(hull) >= 3:
            canvas = [to_canvas(point) for point in hull]
            polygon = " ".join(f"{x:.1f},{y:.1f}" for x, y in [*canvas, canvas[0]])
            parts.append(
                f'<polygon points="{polygon}" fill="{_cluster_hue(label)}" fill-opacity="0.13" '
                f'stroke="{_cluster_hue(label)}" stroke-width="1.2" stroke-opacity="0.8"/>'
            )

    for point, label in zip(coords, labels, strict=True):
        x, y = to_canvas(point)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{_cluster_hue(int(label))}" '
            f'fill-opacity="0.85"/>'
        )

    parts.append(
        _text(
            _MARGIN,
            height - 6,
            f"PCA 2D - axis 1 {explained[0]:.0%} of variance, axis 2 {explained[1]:.0%}; "
            f"hull = convex hull per cluster",
        )
    )
    parts.append(_text(_MARGIN, _MARGIN - 8, f"{title}  ({len(groups)} clusters)"))
    return _svg(width, height, "".join(parts), title)


# ----------------------------------------------------------------- heatmap


def _ramp(fraction: float) -> str:
    """White to blue, used for the heatmap so cell colour reads as one quantity."""
    fraction = min(max(fraction, 0.0), 1.0)
    red = round(247 - 120 * fraction)
    green = round(251 - 70 * fraction)
    blue = round(255 - 10 * fraction)
    return f"#{red:02x}{green:02x}{blue:02x}"


def render_heatmap(
    matrix: np.ndarray,
    rows: Sequence[str],
    columns: Sequence[str],
    title: str,
    row_size: int = 20,
    cell_width: int = 46,
) -> str:
    """Cluster by attribute, each cell the share of that cluster with the attribute."""
    if matrix.shape != (len(rows), len(columns)):
        raise ValueError(f"heatmap matrix {matrix.shape} does not match {len(rows)}x{len(columns)}")
    head = 120
    width = head + cell_width * len(columns) + _MARGIN
    height = _MARGIN + 22 + row_size * len(rows) + _MARGIN
    parts: list[str] = [_text(_MARGIN, 14, f"{title}  (cell = share of cluster)")]
    for index, column in enumerate(columns):
        parts.append(
            _text(
                head + index * cell_width + cell_width / 2,
                _MARGIN + 14,
                column,
                size=9,
                fill="#52606d",
            )
        )
    for row_index, row in enumerate(rows):
        y = _MARGIN + 22 + row_index * row_size
        parts.append(_text(4, y + row_size - 6, row, size=9, fill="#52606d"))
        for column_index in range(len(columns)):
            value = float(matrix[row_index, column_index])
            x = head + column_index * cell_width
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_width - 1}" height="{row_size - 1}" '
                f'fill="{_ramp(value)}" stroke="#ffffff" stroke-width="0.5"/>'
            )
            if value >= 0.5 and row_size >= 16 and cell_width >= 34:
                parts.append(
                    _text(
                        x + cell_width / 2, y + row_size - 7, f"{value:.0%}", size=9, fill="#ffffff"
                    )
                )
    return _svg(width, height, "".join(parts), title)


def cluster_attribute_matrix(
    members: Mapping[int, Sequence[str]],
    tags: Mapping[str, Sequence[str]],
    columns: Sequence[str],
) -> np.ndarray:
    """Share of each cluster carrying each attribute tag.

    Rows are normalised so every row sums to one across the tags present in it: a
    big cluster is not more interesting than a small one, and an unnormalised
    heatmap would rank clusters by size instead of by composition.
    """
    matrix = np.zeros((len(members), len(columns)), dtype=np.float64)
    for row_index, label in enumerate(sorted(members)):
        texts = list(members[label])
        if not texts:
            continue
        counts: dict[str, int] = {}
        for text in texts:
            for tag in tags.get(text, ()):
                if tag in columns:
                    counts[tag] = counts.get(tag, 0) + 1
        total = sum(counts.values()) or 1
        for column_index, column in enumerate(columns):
            matrix[row_index, column_index] = counts.get(column, 0) / total
    return matrix
