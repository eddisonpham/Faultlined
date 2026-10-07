"""Lay clusters out in two dimensions without a numerical stack."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Point = tuple[float, float]

ITERATIONS = 12

MAX_POINTS = 200


@dataclass(frozen=True, slots=True)
class Layout:
    """A 2D projection plus the share of similarity it preserves."""

    points: tuple[Point, ...]
    explained: float
    extent: float
    dropped: int = 0


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _matvec(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> list[float]:
    return [sum(row[j] * vector[j] for j in range(len(vector))) for row in matrix]


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _unit(vector: Sequence[float], size: int) -> list[float]:
    norm = _norm(vector)
    if norm == 0.0:
        return [1.0 / (size**0.5)] * size
    return [value / norm for value in vector]


def _dominant(matrix: Sequence[Sequence[float]]) -> tuple[list[float], float]:
    """Power iteration: the leading eigenvector and its eigenvalue."""
    size = len(matrix)
    vector = _unit([float((index % 7) + 1) for index in range(size)], size)
    value = 0.0
    for _ in range(ITERATIONS):
        updated = _unit(_matvec(matrix, vector), size)
        value = sum(a * b for a, b in zip(updated, _matvec(matrix, vector), strict=True))
        vector = updated
    return vector, value


def _double_centre(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    if not size:
        return matrix
    row_means = [sum(row) / size for row in matrix]
    grand = sum(row_means) / size
    return [
        [matrix[i][j] - row_means[i] - row_means[j] + grand for j in range(size)]
        for i in range(size)
    ]


def project(vectors: Sequence[Sequence[float]], *, limit: int = MAX_POINTS) -> Layout:
    """Project unit vectors onto two dimensions, biggest clusters first."""
    usable = list(vectors)[:limit]
    dropped = max(0, len(vectors) - len(usable))
    size = len(usable)
    if size == 0:
        return Layout(points=(), explained=0.0, extent=1.0, dropped=dropped)
    if size == 1:
        return Layout(points=((0.0, 0.0),), explained=0.0, extent=1.0, dropped=dropped)

    similarity = _double_centre([[_cosine(left, right) for right in usable] for left in usable])
    first, first_value = _dominant(similarity)

    projected = _matvec(similarity, first)
    residual = [
        [similarity[i][j] - projected[i] * first[j] for j in range(size)] for i in range(size)
    ]
    second, second_value = _dominant(residual)
    second_projection = _matvec(residual, second)

    trace = sum(similarity[i][i] for i in range(size))
    points = tuple((projected[i], second_projection[i]) for i in range(size))
    return Layout(
        points=points,
        explained=round((first_value + second_value) / trace, 4) if trace else 0.0,
        extent=max((max(abs(x), abs(y)) for x, y in points), default=1.0) or 1.0,
        dropped=dropped,
    )


def convex_hull(points: Sequence[Point]) -> list[Point]:
    """Andrew's monotone chain."""
    unique = sorted(set(points))
    if len(unique) < 3:
        return unique

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[Point] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]
