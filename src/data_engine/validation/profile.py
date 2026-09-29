"""Declarative, hash-addressed validation profiles (FR-002; ADR 0016).

A profile is a *policy document*, not code: which channels an episode must have, how long
it must be, what its frame rate may be. It is validated when it is constructed, so a
malformed profile cannot be persisted, and it is addressed by the SHA-256 of its canonical
JSON, so "the same profile" is a fact rather than a name comparison.

JSON rather than YAML is a deliberate departure from `architecture/components.md` §6 and is
recorded in ADR 0016: LeRobot datasets already ship `meta/*.json`, the platform already
hashes canonical JSON for idempotency and manifests, and PyYAML would be a dependency
nothing else in the stack justifies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from data_engine.canonical import canonical_json

PROFILE_SCHEMA_VERSION = 1


class InvalidProfile(ValueError):
    """A profile is malformed. Rejected at construction, never persisted."""


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    """An immutable, hash-addressed set of validation constraints."""

    name: str
    version: str
    required_channels: frozenset[str] = frozenset()
    forbidden_channels: frozenset[str] = frozenset()
    min_frames: int | None = None
    max_frames: int | None = None
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    min_fps: float | None = None
    max_fps: float | None = None
    enabled_rules: frozenset[str] | None = None
    """None means every registered rule runs; a set restricts it."""

    content_hash: str = field(default="", compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "required_channels": sorted(self.required_channels),
            "forbidden_channels": sorted(self.forbidden_channels),
            "min_frames": self.min_frames,
            "max_frames": self.max_frames,
            "min_duration_seconds": self.min_duration_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "enabled_rules": (
                sorted(self.enabled_rules) if self.enabled_rules is not None else None
            ),
        }


def _require(document: dict[str, Any], key: str) -> Any:
    if key not in document:
        raise InvalidProfile(f"profile is missing required key {key!r}")
    return document[key]


def _positive(document: dict[str, Any], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidProfile(f"{key} must be a positive integer, got {value!r}")
    return value


def _non_negative_float(document: dict[str, Any], key: str) -> float | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise InvalidProfile(f"{key} must be a non-negative number, got {value!r}")
    return float(value)


def _channel_set(document: dict[str, Any], key: str) -> frozenset[str]:
    value = document.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise InvalidProfile(f"{key} must be a list of non-empty strings")
    return frozenset(value)


def profile_from_dict(document: dict[str, Any]) -> ValidationProfile:
    """Build a profile, rejecting anything malformed at the point of construction."""
    if not isinstance(document, dict):
        raise InvalidProfile("profile must be a JSON object")
    name = _require(document, "name")
    version = _require(document, "version")
    if not isinstance(name, str) or not name:
        raise InvalidProfile("name must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise InvalidProfile("version must be a non-empty string")

    enabled = document.get("enabled_rules")
    if enabled is not None and (
        not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled)
    ):
        raise InvalidProfile("enabled_rules must be a list of rule names when present")

    required = _channel_set(document, "required_channels")
    forbidden = _channel_set(document, "forbidden_channels")
    overlap = required & forbidden
    if overlap:
        raise InvalidProfile(
            f"channels cannot be both required and forbidden: {', '.join(sorted(overlap))}"
        )

    profile = ValidationProfile(
        name=name,
        version=version,
        required_channels=required,
        forbidden_channels=forbidden,
        min_frames=_positive(document, "min_frames"),
        max_frames=_positive(document, "max_frames"),
        min_duration_seconds=_non_negative_float(document, "min_duration_seconds"),
        max_duration_seconds=_non_negative_float(document, "max_duration_seconds"),
        min_fps=_non_negative_float(document, "min_fps"),
        max_fps=_non_negative_float(document, "max_fps"),
        enabled_rules=None if enabled is None else frozenset(enabled),
    )
    _check_bounds(profile)
    return profile


def _check_bounds(profile: ValidationProfile) -> None:
    for low, high, label in (
        (profile.min_frames, profile.max_frames, "frames"),
        (profile.min_duration_seconds, profile.max_duration_seconds, "duration_seconds"),
        (profile.min_fps, profile.max_fps, "fps"),
    ):
        if low is not None and high is not None and low > high:
            raise InvalidProfile(f"min_{label} ({low}) is greater than max_{label} ({high})")


def profile_from_json(raw: str) -> ValidationProfile:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidProfile(f"profile is not valid JSON: {exc}") from exc
    return profile_from_dict(document)


def profile_hash(profile: ValidationProfile) -> str:
    """Content address: sha256 over the profile's canonical JSON."""
    return hashlib.sha256(canonical_json(profile.to_dict())).hexdigest()


__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "InvalidProfile",
    "ValidationProfile",
    "profile_from_dict",
    "profile_from_json",
    "profile_hash",
]
