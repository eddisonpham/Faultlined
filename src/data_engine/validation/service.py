"""Runs a profile against an episode and records the outcome (FR-002, FR-003)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data_engine.ingest.readers.base import ChannelStats, EpisodeExtraction
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.observability.reason_codes import ReasonCode
from data_engine.validation.profile import ValidationProfile, profile_hash
from data_engine.validation.rules import Validatable, Violation, ViolationCode, rules_for

INGESTED = "ingested"
VALID = "valid"
QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class _Subject:
    episode: EpisodeExtraction
    profile: ValidationProfile
    samples: dict[str, list[float]]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The outcome of one (episode, profile) evaluation. Immutable once recorded."""

    passed: bool
    reason_codes: tuple[str, ...]
    violations: tuple[Violation, ...]
    profile_hash: str
    profile_name: str
    profile_version: str
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "violations": [violation.to_dict() for violation in self.violations],
            "profile_hash": self.profile_hash,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "duration_seconds": self.duration_seconds,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
            "profile_hash": self.profile_hash,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
        }


class ValidationService:
    """Evaluate episodes against profiles; persist the immutable result.

    Re-validation after a profile fix is a *new* result row, not an update
    (`storage.md` §1): two results for the same episode under different profiles are both
    true statements about the world, and overwriting would destroy the audit trail that
    FR-003 asks for.
    """

    def __init__(
        self,
        catalog: Any,
        *,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.catalog = catalog
        self.metrics = metrics or RuntimeMetrics()

    def evaluate(
        self,
        extraction: EpisodeExtraction,
        profile: ValidationProfile,
        *,
        samples: dict[str, list[float]] | None = None,
    ) -> ValidationResult:
        """Run every enabled rule; never raise for a bad episode, only for a bad profile."""
        subject = _Subject(episode=extraction, profile=profile, samples=samples or {})
        violations: list[Violation] = []
        started = time.perf_counter()
        for name, rule in rules_for(profile):
            try:
                violations.extend(_check(rule, subject))
            except Exception as exc:  # a rule bug is a failure, not a pass
                violations.append(
                    Violation(
                        ViolationCode.RULE_ERROR,
                        f"rule {name!r} raised {type(exc).__name__}: {exc}",
                    )
                )
        duration_seconds = time.perf_counter() - started

        codes = tuple(sorted({violation.code.value for violation in violations}))
        result = ValidationResult(
            passed=not violations,
            reason_codes=() if not violations else (ReasonCode.VALIDATION_FAILED.value, *codes),
            violations=tuple(violations),
            profile_hash=profile_hash(profile),
            profile_name=profile.name,
            profile_version=profile.version,
            duration_seconds=duration_seconds,
        )
        self.metrics.stage_duration(
            duration_seconds, stage="validate", status=VALID if result.passed else QUARANTINED
        )
        return result

    def record(
        self,
        episode_id: str,
        extraction: EpisodeExtraction,
        profile: ValidationProfile,
        *,
        samples: dict[str, list[float]] | None = None,
    ) -> ValidationResult:
        """Evaluate, then persist the result and move the episode's state."""
        result = self.evaluate(extraction, profile, samples=samples)
        self._persist(episode_id, result)
        return result

    def validate_stored_episode(
        self, episode_id: str, profile: ValidationProfile
    ) -> ValidationResult:
        """Validate an episode already in the catalog, from its stored metadata.

        This is the path FR-003 asks for: re-validating after a profile fix must not
        re-ingest bytes. Because the reader already summarised the episode at ingest
        time, validating it is a pure function of the catalog row - no file is opened,
        and the same profile on the same metadata always gives the same answer.
        """
        episode = self.catalog.get_episode(episode_id)
        if episode is None:
            raise KeyError(episode_id)
        result = self.evaluate(extraction_from_metadata(episode["metadata"]), profile)
        self._persist(episode_id, result)
        return result

    def _persist(self, episode_id: str, result: ValidationResult) -> None:
        self.catalog.record_validation(
            episode_id=episode_id,
            profile_hash=result.profile_hash,
            profile_name=result.profile_name,
            profile_version=result.profile_version,
            passed=result.passed,
            reason_codes=list(result.reason_codes),
            violations=[violation.to_dict() for violation in result.violations],
        )


def extraction_from_metadata(metadata: dict[str, Any]) -> EpisodeExtraction:
    """Rebuild the summary a reader produced, from the catalog row that stored it.

    Round-trips `EpisodeExtraction.metadata()`. Anything missing degrades to a value
    that trips a rule rather than raising: an episode with no readable frame count should
    fail validation, not crash it.
    """
    stats = metadata.get("channel_stats") or {}
    channels = tuple(
        ChannelStats(
            name=str(name),
            dtype=str((values or {}).get("dtype") or "unknown"),
            count=int((values or {}).get("count") or 0),
            min=(values or {}).get("min"),
            max=(values or {}).get("max"),
            mean=(values or {}).get("mean"),
            std=(values or {}).get("std"),
        )
        for name, values in sorted(stats.items())
    )
    return EpisodeExtraction(
        format=str(metadata.get("format") or "unknown"),
        format_version=str(metadata.get("format_version") or "unknown"),
        episode_key=str(metadata.get("episode_key") or ""),
        source_path=Path("."),
        robot_type=str(metadata.get("robot") or "unknown"),
        task=str(metadata.get("task") or ""),
        tasks=tuple(metadata.get("tasks") or ()),
        frame_count=int(metadata.get("frame_count") or 0),
        duration_seconds=float(metadata.get("duration_seconds") or 0.0),
        fps=metadata.get("fps"),
        channels=channels,
        dataset=dict(metadata.get("dataset") or {}),
    )


def _check(rule: Any, subject: Validatable) -> list[Violation]:
    found = rule(subject)
    return [item for item in found if isinstance(item, Violation)]


__all__ = [
    "INGESTED",
    "QUARANTINED",
    "VALID",
    "ValidationResult",
    "ValidationService",
    "extraction_from_metadata",
]
