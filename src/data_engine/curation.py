"""Curation vocabulary shared by the catalog, the API, and the web layer."""

from __future__ import annotations

EPISODE_STATES: tuple[str, ...] = ("", "ingested", "valid", "quarantined")
EPISODE_FLAGS: tuple[str, ...] = ("", "jerky", "stalled", "short", "long")

REASON_CODES: frozenset[str] = frozenset(
    {"TOO_FEW_FRAMES", "VALIDATION_FAILED", "MISSING_CHANNELS", "TOO_SHORT", "TOO_LONG"}
)

EPISODE_FRAMES_SQL = "({prefix}metadata->>'frame_count')::bigint"


def episode_predicates(
    state: str | None, flag: str | None, *, prefix: str = ""
) -> tuple[list[str], list[object], str]:
    """SQL clauses, parameters, and ordering for one episode curation view."""
    where: list[str] = []
    params: list[object] = []
    order = f"{prefix}created_at DESC"
    if state:
        where.append(f"{prefix}state = %s")
        params.append(state)
    if flag == "jerky":
        where.append("q.verdict = 'jerky'")
        order = "q.jerk_score DESC"
    elif flag == "stalled":
        where.append("q.stall_ratio >= 0.5")
        order = "q.stall_ratio DESC"
    elif flag == "short":
        order = f"COALESCE({EPISODE_FRAMES_SQL.format(prefix=prefix)}, 0) ASC"
    elif flag == "long":
        order = f"COALESCE({EPISODE_FRAMES_SQL.format(prefix=prefix)}, 0) DESC"
    return where, params, order
