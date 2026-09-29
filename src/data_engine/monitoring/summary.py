"""Deterministic incident rendering.

The summary is *rendered from the evidence, not generated*. Three consequences,
all of them the point:

* It is deterministic and offline. No network call, no quota, no credential, and
  no way for the notifier to fail because a provider is down.
* It cannot assert a cause the evidence does not contain, because every sentence
  is assembled from fields that are stored alongside it.
* It is instant and free, which means a slow or expensive explanation can never
  be the reason an incident is delayed.

A future LLM enrichment stage would take exactly this evidence bundle as input
and rewrite this text — but it could not change what an incident *is*, because
detection has already happened by the time anything is rendered (ADR 0020).

Every line follows the same contract, taken from the afk rule: **actionable
without opening the laptop, or it does not claim to be.** "Unusual metric activity
detected" is not a summary. "3 of 200 episodes quarantined by `staged-strict`;
rate is 4x the usual" is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from data_engine.monitoring.signals import Label, NotifyClass, Severity

GIB = 1024**3


def render_summary(signal_label: str, evidence: Mapping[str, Any], detail: str) -> str:
    """One line naming the signal, the numbers, and the scope."""
    renderer = _RENDERERS.get(signal_label, _generic)
    return renderer(evidence, detail)


def _generic(_evidence: Mapping[str, Any], detail: str) -> str:
    return detail


def _contract_breach(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"Run produced {evidence.get('valid_episodes', '?')} valid episodes; "
        f"{evidence.get('expected_episodes', '?')} were expected "
        f"({evidence.get('shortfall', '?')} short, state {evidence.get('job_state', '?')})"
    )


def _partial_success(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"Run finished but only {float(evidence.get('observed_valid_fraction', 0)):.0%} of "
        f"{evidence.get('produced_episodes', '?')} episodes are valid; declared floor was "
        f"{float(evidence.get('expected_valid_fraction', 0)):.0%}"
    )


def _worker_lost(evidence: Mapping[str, Any], _detail: str) -> str:
    age = evidence.get("heartbeat_age_seconds")
    if age is None:
        return "No worker heartbeat seen while jobs are queued or running"
    return (
        f"No worker heartbeat for {float(age):.0f}s (limit "
        f"{float(evidence.get('threshold_seconds', 0)):.0f}s) with "
        f"{int(float(evidence.get('queue_depth_queued', 0)))} queued and "
        f"{int(float(evidence.get('queue_depth_running', 0)))} running. "
        "Likely laptop suspend or a dead worker."
    )


def _run_stalled(evidence: Mapping[str, Any], _detail: str) -> str:
    age = float(evidence.get("age_seconds", 0))
    job = f"Job {evidence.get('job_id')} ({evidence.get('job_type')}) has run {age:.0f}s"
    if evidence.get("deadline_passed"):
        return f"{job}, past its deadline. The night is being wasted."
    return (
        f"{job} with no episodes produced. The night is being wasted — cancel it, or check "
        "whether the source is still being written."
    )


def _disk_pressure(evidence: Mapping[str, Any], _detail: str) -> str:
    free = float(evidence.get("disk_free_bytes", 0))
    floor = float(evidence.get("threshold_bytes", 0))
    return (
        f"{free / GIB:.1f} GiB free, below the {floor / GIB:.0f} GiB floor. Artifact writes "
        "will fail and the work in progress will be lost."
    )


def _database_unreachable(_evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        "Catalog is unreachable. Jobs cannot be claimed and the monitor is running blind; "
        "nothing else can be trusted until it returns."
    )


def _clock_drift(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"Gap of {float(evidence.get('max_gap_seconds', 0)):.1f}s inside an episode from "
        f"{evidence.get('source') or 'an unknown source'}; camera and joint streams are not "
        "aligned, so these episodes are unusable for training"
    )


def _quarantine_rate_high(evidence: Mapping[str, Any], _detail: str) -> str:
    rate = f"{float(evidence.get('quarantine_rate', 0)):.0%} of evaluated episodes are quarantined"
    if evidence.get("basis") == "absolute" or evidence.get("baseline_median") is None:
        return f"{rate}, above the {float(evidence.get('absolute_max', 0)):.0%} hard bound"
    return (
        f"{rate}, {float(evidence.get('robust_z', 0)):.1f} robust sigmas above the usual "
        f"{float(evidence.get('baseline_median', 0)):.0%}"
    )


def _quality_shift(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"{evidence.get('feature')} is {float(evidence.get('value', 0)):.3f}, "
        f"{float(evidence.get('robust_z', 0)):.1f} robust sigmas above the usual "
        f"{float(evidence.get('baseline_median', 0)):.3f} over "
        f"{evidence.get('observations', 0)} windows. Datasets built now will inherit this."
    )


def _frame_count_collapse(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"Median episode length is {float(evidence.get('value', 0)):.0f} frames, "
        f"{float(evidence.get('ratio_to_center', 0)):.0%} of the usual "
        f"{float(evidence.get('baseline_center', 0)):.0f}. A camera has probably dropped rate."
    )


def _repeated_read_failure(evidence: Mapping[str, Any], _detail: str) -> str:
    return (
        f"{int(float(evidence.get('failure_count', 0)))} read/parse failures with reason "
        f"{evidence.get('reason_code')} in one window"
    )


def _resource_degraded(evidence: Mapping[str, Any], _detail: str) -> str:
    parts = []
    if evidence.get("cpu_percent") is not None:
        parts.append(f"cpu {float(evidence['cpu_percent']):.0f}%")
    if evidence.get("memory_used_ratio") is not None:
        parts.append(f"memory {float(evidence['memory_used_ratio']):.0%}")
    return f"Host under pressure ({', '.join(parts)}). Runs will slow down; nothing has failed yet."


def _metric_shift(evidence: Mapping[str, Any], _detail: str) -> str:
    shifted = evidence.get("shifted") or []
    # "sigmas" rather than the sigma glyph: this text is read in terminals, and a
    # cp1252 console cannot encode the character.
    parts = ", ".join(
        f"{item['feature']}={float(item['value']):.4g} ({float(item['robust_z']):+.1f} sigmas)"
        for item in shifted
    )
    return f"{len(shifted)} signals moved together — {parts}"


def _time_missed(evidence: Mapping[str, Any], _detail: str) -> str:
    if evidence.get("overdue_seconds") is not None:
        return (
            f"Job {evidence.get('job_id', '')} passed its deadline by "
            f"{float(evidence['overdue_seconds']):.0f}s and is still {evidence.get('job_state')}"
        )
    return (
        f"Job {evidence.get('job_id', '')} took {float(evidence.get('duration_seconds', 0)):.0f}s, "
        f"over the declared {float(evidence.get('max_duration_seconds', 0)):.0f}s"
    )


_RENDERERS = {
    Label.CONTRACT_BREACH.value: _contract_breach,
    Label.PARTIAL_SUCCESS.value: _partial_success,
    Label.WORKER_LOST.value: _worker_lost,
    Label.RUN_STALLED.value: _run_stalled,
    Label.DISK_PRESSURE.value: _disk_pressure,
    Label.DATABASE_UNREACHABLE.value: _database_unreachable,
    Label.CLOCK_DRIFT.value: _clock_drift,
    Label.QUARANTINE_RATE_HIGH.value: _quarantine_rate_high,
    Label.QUALITY_SHIFT.value: _quality_shift,
    Label.FRAME_COUNT_COLLAPSE.value: _frame_count_collapse,
    Label.REPEATED_READ_FAILURE.value: _repeated_read_failure,
    Label.RESOURCE_DEGRADED.value: _resource_degraded,
    Label.METRIC_SHIFT.value: _metric_shift,
    Label.TIME_MISSED.value: _time_missed,
}


def render_digest(incidents: Sequence[Mapping[str, Any]]) -> str:
    """Batch the queue into one morning-read summary.

    Notify-class incidents come first and are never batched away; queue-class
    incidents follow in a single line each. An absent operator gets the wasted
    night up front and everything else underneath it.
    """
    if not incidents:
        return "No incidents."
    ordered = sorted(
        incidents,
        key=lambda row: (
            0 if str(row.get("notify_class")) == NotifyClass.NOTIFY.value else 1,
            -_severity_rank(str(row.get("severity"))),
            str(row.get("first_seen") or ""),
        ),
    )
    interrupting = [row for row in ordered if row.get("notify_class") == NotifyClass.NOTIFY.value]
    lines: list[str] = []
    if interrupting:
        lines.append(f"{len(interrupting)} need attention overnight:")
        lines.extend(f"  - [{row['label']}] {row['summary']}" for row in interrupting)
    remaining = [row for row in ordered if row not in interrupting]
    if remaining:
        lines.append(f"{len(remaining)} queued:")
        lines.extend(
            f"  - [{row['label']}] {row['summary']} (x{row.get('occurrence_count', 1)})"
            for row in remaining
        )
    return "\n".join(lines)


def _severity_rank(value: str) -> int:
    order = [
        Severity.INFO.value,
        Severity.MEDIUM.value,
        Severity.HIGH.value,
        Severity.CRITICAL.value,
    ]
    return order.index(value) if value in order else 0


def render_notify(incidents: Sequence[Mapping[str, Any]]) -> str:
    """The body a notifier would deliver. Rendering only; nothing is sent.

    Kept separate from :func:`render_digest` so the dry-run path and a future
    delivery stage share one implementation of "what would the owner read".
    """
    if not incidents:
        return ""
    header = (
        "Faultlined: work needs attention"
        if len(incidents) == 1
        else (f"Faultlined: {len(incidents)} things need attention")
    )
    body = [f"  [{row['label']}] {row['summary']}" for row in incidents]
    footer = "Open /ui/incidents to acknowledge."
    return "\n".join([header, *body, "", footer])
