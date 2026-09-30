"""Curation vocabulary shared by the catalog, the API, and the web layer.

The episode curation views (`state` / `flag`) and their SQL predicates are one
concept with three consumers, so they live here rather than in the web pages that
happened to define them first. Keeping the predicate in one place is what lets
`GET /api/v1/episodes`, `GET /api/v1/episodes/export`, and a saved slice's manifest
agree by construction instead of by copy.
"""

from __future__ import annotations

EPISODE_STATES: tuple[str, ...] = ("", "ingested", "valid", "quarantined")
EPISODE_FLAGS: tuple[str, ...] = ("", "jerky", "stalled", "short", "long")

# Reason codes the validator emits (observability/reason_codes.py owns the registry);
# the failures read view accepts these as filters and rejects anything else with 422.
REASON_CODES: frozenset[str] = frozenset(
    {"TOO_FEW_FRAMES", "VALIDATION_FAILED", "MISSING_CHANNELS", "TOO_SHORT", "TOO_LONG"}
)

#: SQL for **the episode's own length**, from the metadata the reader produced.
#:
#: Not `episode_quality.frame_count`. That column is how many frames the quality
#: analysis actually saw, which for a streaming reader is a bounded, decimated
#: sample: a 72 600-message MCAP log is analysed over 1024 frames, so reading its
#: "length" from the quality table under-reports it by ~70x. The two numbers are
#: equal by coincidence for a non-streaming reader, which is exactly why the
#: coincidence survived until a streaming format existed.
EPISODE_FRAMES_SQL = "({prefix}metadata->>'frame_count')::bigint"


def episode_predicates(
    state: str | None, flag: str | None, *, prefix: str = ""
) -> tuple[list[str], list[object], str]:
    """SQL clauses, parameters, and ordering for one episode curation view.

    `jerky`/`stalled` are predicates on the quality signals and rank by the signal
    they filter on; `short`/`long` are ordering only, so the tails of the length
    distribution surface first without excluding anything. The default is newest
    first. `prefix` qualifies the episode columns for a join that already has them
    in scope.
    """
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
