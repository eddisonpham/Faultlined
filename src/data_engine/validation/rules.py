"""Validation rules and their stable violation codes (FR-002).

Each rule is a small function over the reader's `EpisodeExtraction` plus the profile. They
never touch the artifact bytes: validation is O(metadata), so it can run on every episode
(NFR-002), and there is no second implementation of the LeRobot layout to drift out of sync
with the reader. A caller that wants a per-frame check passes a sample; the default path
does not re-read the file (see ADR 0016).

A rule that **raises** is a failure, not a pass. Rule bugs are recorded as
`VALIDATION_RULE_ERROR` and quarantine the episode, because a validator that silently passes
data it could not check is worse than one that stops the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from data_engine.ingest.readers.base import EpisodeExtraction
from data_engine.validation.profile import ValidationProfile


class ViolationCode(StrEnum):
    """Stable rule-violation codes. Add, never rename (ADR 0009)."""

    REQUIRED_CHANNEL_MISSING = "REQUIRED_CHANNEL_MISSING"
    FORBIDDEN_CHANNEL_PRESENT = "FORBIDDEN_CHANNEL_PRESENT"
    TOO_FEW_FRAMES = "TOO_FEW_FRAMES"
    TOO_MANY_FRAMES = "TOO_MANY_FRAMES"
    DURATION_TOO_SHORT = "DURATION_TOO_SHORT"
    DURATION_TOO_LONG = "DURATION_TOO_LONG"
    FRAME_RATE_OUT_OF_BOUNDS = "FRAME_RATE_OUT_OF_BOUNDS"
    FRAME_RATE_UNDECLARED = "FRAME_RATE_UNDECLARED"
    NON_FINITE_VALUES = "NON_FINITE_VALUES"
    EMPTY_EPISODE = "EMPTY_EPISODE"
    UNDECLARED_TASK = "UNDECLARED_TASK"
    RULE_ERROR = "RULE_ERROR"


@dataclass(frozen=True, slots=True)
class Violation:
    code: ViolationCode
    detail: str
    channel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail, "channel": self.channel}


class Validatable(Protocol):
    """What a rule is allowed to see."""

    @property
    def episode(self) -> EpisodeExtraction: ...

    @property
    def profile(self) -> ValidationProfile: ...

    @property
    def samples(self) -> dict[str, list[float]]: ...


Rule = Callable[[Validatable], list[Violation]]


# --------------------------------------------------------------- channel presence


def required_channels(subject: Validatable) -> list[Violation]:
    present = {channel.name for channel in subject.episode.channels}
    return [
        Violation(ViolationCode.REQUIRED_CHANNEL_MISSING, f"channel {name!r} is absent", name)
        for name in sorted(subject.profile.required_channels - present)
    ]


def forbidden_channels(subject: Validatable) -> list[Violation]:
    present = {channel.name for channel in subject.episode.channels}
    return [
        Violation(
            ViolationCode.FORBIDDEN_CHANNEL_PRESENT, f"channel {name!r} must not be present", name
        )
        for name in sorted(subject.profile.forbidden_channels & present)
    ]


# ------------------------------------------------------------------ episode shape


def frame_count(subject: Validatable) -> list[Violation]:
    found = [
        violation
        for violation in (
            _below(
                ViolationCode.TOO_FEW_FRAMES,
                "frames",
                subject.episode.frame_count,
                subject.profile.min_frames,
            ),
            _above(
                ViolationCode.TOO_MANY_FRAMES,
                "frames",
                subject.episode.frame_count,
                subject.profile.max_frames,
            ),
        )
        if violation is not None
    ]
    if subject.episode.frame_count == 0:
        found.append(Violation(ViolationCode.EMPTY_EPISODE, "the episode has no frames"))
    return found


def duration(subject: Validatable) -> list[Violation]:
    found = subject.episode.duration_seconds
    return [
        violation
        for violation in (
            _below(
                ViolationCode.DURATION_TOO_SHORT,
                "duration_seconds",
                found,
                subject.profile.min_duration_seconds,
            ),
            _above(
                ViolationCode.DURATION_TOO_LONG,
                "duration_seconds",
                found,
                subject.profile.max_duration_seconds,
            ),
        )
        if violation is not None
    ]


def frame_rate(subject: Validatable) -> list[Violation]:
    fps = subject.episode.fps
    if fps is None:
        if subject.profile.min_fps is not None or subject.profile.max_fps is not None:
            return [
                Violation(
                    ViolationCode.FRAME_RATE_UNDECLARED,
                    "the source declares no fps, so a bounded frame rate cannot be checked",
                )
            ]
        return []
    return [
        violation
        for violation in (
            _below(ViolationCode.FRAME_RATE_OUT_OF_BOUNDS, "fps", fps, subject.profile.min_fps),
            _above(ViolationCode.FRAME_RATE_OUT_OF_BOUNDS, "fps", fps, subject.profile.max_fps),
        )
        if violation is not None
    ]


def task_declared(subject: Validatable) -> list[Violation]:
    if subject.episode.task.strip():
        return []
    return [Violation(ViolationCode.UNDECLARED_TASK, "the episode carries no task string")]


def finite_values(subject: Validatable) -> list[Violation]:
    """NaN/Inf in the statistics a format published, or in a caller-supplied sample."""
    found: list[Violation] = []
    for channel in subject.episode.channels:
        for value in (channel.min, channel.max, channel.mean, channel.std):
            if value is not None and value != value:  # NaN
                found.append(
                    Violation(
                        ViolationCode.NON_FINITE_VALUES, "NaN in published statistics", channel.name
                    )
                )
                break
    for name, values in subject.samples.items():
        if any(value != value for value in values):
            found.append(Violation(ViolationCode.NON_FINITE_VALUES, "NaN in the sample", name))
    return found


# -------------------------------------------------------------------- registry

REGISTRY: dict[str, Rule] = {
    "required_channels": required_channels,
    "forbidden_channels": forbidden_channels,
    "frame_count": frame_count,
    "duration": duration,
    "frame_rate": frame_rate,
    "task_declared": task_declared,
    "finite_values": finite_values,
}


def rules_for(profile: ValidationProfile) -> list[tuple[str, Rule]]:
    """Rules a profile enables, in a stable order so results are reproducible."""
    names = sorted(
        REGISTRY if profile.enabled_rules is None else profile.enabled_rules & REGISTRY.keys()
    )
    return [(name, REGISTRY[name]) for name in names]


def _below(
    code: ViolationCode, unit: str, value: float | int, limit: float | int | None
) -> Violation | None:
    if limit is not None and value < limit:
        return Violation(code, f"{unit} {value} is below the required {limit}")
    return None


def _above(
    code: ViolationCode, unit: str, value: float | int, limit: float | int | None
) -> Violation | None:
    if limit is not None and value > limit:
        return Violation(code, f"{unit} {value} exceeds the allowed {limit}")
    return None


__all__ = ["REGISTRY", "Rule", "Validatable", "Violation", "ViolationCode", "rules_for"]
