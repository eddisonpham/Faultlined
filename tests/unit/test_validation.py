"""Validation unit tests: profile construction, rules, and the service (FR-002/003)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_engine.ingest.readers.base import ChannelStats, EpisodeExtraction
from data_engine.validation.profile import (
    InvalidProfile,
    ValidationProfile,
    profile_from_dict,
    profile_from_json,
    profile_hash,
)
from data_engine.validation.rules import REGISTRY, ViolationCode
from data_engine.validation.service import (
    ValidationService,
    extraction_from_metadata,
)


def _profile(**overrides: Any) -> ValidationProfile:
    document: dict[str, Any] = {"name": "manipulation", "version": "1"}
    document.update(overrides)
    return profile_from_dict(document)


def _episode(
    *,
    task: str = "pick",
    frames: int = 300,
    duration: float = 10.0,
    fps: float | None = 30.0,
    channels: tuple[str, ...] = ("action", "observation.state"),
) -> EpisodeExtraction:
    return EpisodeExtraction(
        format="lerobot-v3",
        format_version="v3.0",
        episode_key="episode_index=0",
        source_path=Path("shard.parquet"),
        robot_type="so100_follower",
        task=task,
        tasks=(task,) if task else (),
        frame_count=frames,
        duration_seconds=duration,
        fps=fps,
        channels=tuple(
            ChannelStats(name=name, dtype="list<float>", count=frames, min=0.0, max=1.0, mean=0.5)
            for name in channels
        ),
    )


@pytest.mark.unit
def test_profile_hash_is_stable_and_order_independent() -> None:
    """A profile's identity must not depend on the order its author typed keys in."""
    first = _profile(required_channels=["action", "observation.state"], min_fps=20)
    second = _profile(min_fps=20, required_channels=["observation.state", "action"])

    assert profile_hash(first) == profile_hash(second)
    assert len(profile_hash(first)) == 64


@pytest.mark.unit
def test_profile_hash_changes_when_a_constraint_changes() -> None:
    assert profile_hash(_profile(min_fps=20)) != profile_hash(_profile(min_fps=21))


@pytest.mark.unit
def test_profile_round_trips_through_canonical_json() -> None:
    original = _profile(required_channels=["action"], max_frames=1000)
    restored = profile_from_dict(original.to_dict())

    assert profile_hash(restored) == profile_hash(original)


@pytest.mark.unit
def test_profile_rejects_a_document_with_no_identity() -> None:
    with pytest.raises(InvalidProfile, match="missing required key 'name'"):
        profile_from_dict({"version": "1"})
    with pytest.raises(InvalidProfile, match="missing required key 'version'"):
        profile_from_dict({"name": "x"})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"name": "", "version": "1"}, "name must be"),
        ({"name": "x", "version": 1}, "version must be"),
        ({"name": "x", "version": "1", "min_frames": 0}, "positive integer"),
        ({"name": "x", "version": "1", "min_frames": -1}, "positive integer"),
        ({"name": "x", "version": "1", "min_fps": -1}, "non-negative"),
        ({"name": "x", "version": "1", "required_channels": "action"}, "list of"),
        ({"name": "x", "version": "1", "required_channels": [1]}, "list of"),
        ({"name": "x", "version": "1", "enabled_rules": "duration"}, "list of rule names"),
        ({"name": "x", "version": "1", "min_frames": 10, "max_frames": 5}, "greater than"),
        ({"name": "x", "version": "1", "min_fps": 40, "max_fps": 20}, "greater than"),
    ],
)
def test_profile_rejects_a_malformed_document(document: dict[str, Any], message: str) -> None:
    """A bad profile must be unconstructable, so it can never be persisted."""
    with pytest.raises(InvalidProfile, match=message):
        profile_from_dict(document)


@pytest.mark.unit
def test_profile_rejects_a_channel_that_is_required_and_forbidden() -> None:
    with pytest.raises(InvalidProfile, match="both required and forbidden"):
        profile_from_dict(
            {"name": "x", "version": "1", "required_channels": ["a"], "forbidden_channels": ["a"]}
        )


@pytest.mark.unit
def test_profile_rejects_text_that_is_not_json() -> None:
    with pytest.raises(InvalidProfile, match="not valid JSON"):
        profile_from_json("{oops")


class _Catalog:
    def __init__(self) -> None:
        self.episodes: dict[str, dict[str, Any]] = {}
        self.recorded: list[dict[str, Any]] = []
        self.profiles: list[ValidationProfile] = []

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return self.episodes.get(episode_id)

    def record_validation(self, **kwargs: Any) -> dict[str, Any]:
        self.recorded.append(kwargs)
        return {"id": len(self.recorded), "passed": kwargs["passed"]}

    def register_validation_profile(self, profile: ValidationProfile) -> dict[str, Any]:
        self.profiles.append(profile)
        return {"hash": profile_hash(profile), "created": True}


def _evaluate(episode: EpisodeExtraction, profile: ValidationProfile, **kwargs: Any) -> Any:
    return ValidationService(_Catalog()).evaluate(episode, profile, **kwargs)


@pytest.mark.unit
def test_a_conforming_episode_passes_with_no_reason_codes() -> None:
    result = _evaluate(_episode(), _profile(required_channels=["action"], min_fps=20, max_fps=40))

    assert result.passed is True
    assert result.reason_codes == ()
    assert result.profile_name == "manipulation"


@pytest.mark.unit
def test_a_missing_required_channel_is_reported_by_name() -> None:
    result = _evaluate(
        _episode(channels=("action",)), _profile(required_channels=["action", "gripper"])
    )

    assert result.passed is False
    assert ViolationCode.REQUIRED_CHANNEL_MISSING.value in result.reason_codes
    assert "VALIDATION_FAILED" in result.reason_codes
    missing = [v for v in result.violations if v.code == ViolationCode.REQUIRED_CHANNEL_MISSING]
    assert [v.channel for v in missing] == ["gripper"]


@pytest.mark.unit
def test_a_forbidden_channel_is_reported() -> None:
    result = _evaluate(
        _episode(channels=("action", "observation.images.up")),
        _profile(forbidden_channels=["observation.images.up"]),
    )

    assert result.passed is False
    assert ViolationCode.FORBIDDEN_CHANNEL_PRESENT.value in result.reason_codes


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"min_frames": 500}, ViolationCode.TOO_FEW_FRAMES),
        ({"max_frames": 100}, ViolationCode.TOO_MANY_FRAMES),
        ({"min_duration_seconds": 30}, ViolationCode.DURATION_TOO_SHORT),
        ({"max_duration_seconds": 1}, ViolationCode.DURATION_TOO_LONG),
        ({"min_fps": 60}, ViolationCode.FRAME_RATE_OUT_OF_BOUNDS),
    ],
)
def test_numeric_bounds_are_enforced(overrides: dict[str, Any], code: ViolationCode) -> None:
    result = _evaluate(_episode(), _profile(**overrides))

    assert result.passed is False
    assert code.value in result.reason_codes


@pytest.mark.unit
def test_an_empty_episode_is_reported_as_empty() -> None:
    result = _evaluate(_episode(frames=0, duration=0.0), _profile())

    assert ViolationCode.EMPTY_EPISODE.value in result.reason_codes


@pytest.mark.unit
def test_a_bounded_frame_rate_needs_a_declared_one() -> None:
    """Failing closed beats silently skipping a check the source cannot support."""
    bounded = _evaluate(_episode(fps=None), _profile(min_fps=10))
    assert ViolationCode.FRAME_RATE_UNDECLARED.value in bounded.reason_codes

    unbounded = _evaluate(_episode(fps=None), _profile(min_frames=1))
    assert unbounded.passed is True, "an unbounded profile must not care"


@pytest.mark.unit
def test_an_episode_with_no_task_is_reported() -> None:
    result = _evaluate(_episode(task=""), _profile())
    assert ViolationCode.UNDECLARED_TASK.value in result.reason_codes


@pytest.mark.unit
def test_nan_in_published_statistics_is_reported() -> None:
    episode = EpisodeExtraction(
        format="lerobot-v3",
        format_version="v3.0",
        episode_key="episode_index=0",
        source_path=Path("s.parquet"),
        robot_type="arm",
        task="pick",
        tasks=("pick",),
        frame_count=10,
        duration_seconds=1.0,
        channels=(ChannelStats(name="action", dtype="f32", count=10, min=float("nan"), max=1.0),),
    )
    result = _evaluate(episode, _profile())

    assert ViolationCode.NON_FINITE_VALUES.value in result.reason_codes
    assert "action" in {v.channel for v in result.violations}


@pytest.mark.unit
def test_nan_in_a_supplied_sample_is_reported() -> None:
    result = _evaluate(_episode(), _profile(), samples={"action": [0.0, float("nan")]})

    assert ViolationCode.NON_FINITE_VALUES.value in result.reason_codes


@pytest.mark.unit
def test_a_rule_that_raises_fails_closed() -> None:
    """A validator that passes data it could not check is worse than one that stops."""

    def exploding(_subject: Any) -> list[Any]:
        raise RuntimeError("rule bug")

    original = REGISTRY["duration"]
    REGISTRY["duration"] = exploding
    try:
        result = _evaluate(_episode(), _profile(min_duration_seconds=5))
    finally:
        REGISTRY["duration"] = original

    assert result.passed is False
    assert ViolationCode.RULE_ERROR.value in result.reason_codes
    assert any("rule bug" in v.detail for v in result.violations)


@pytest.mark.unit
def test_only_enabled_rules_run() -> None:
    profile = _profile(required_channels=["missing"], enabled_rules=["duration"])

    result = _evaluate(_episode(), profile)

    assert result.passed is True, "required_channels was not enabled, so it must not run"
    assert "duration" in [name for name, _ in REGISTRY.items()]


@pytest.mark.unit
def test_an_unknown_rule_name_is_ignored_rather_than_fatal() -> None:
    """A profile from a newer engine must still validate; unknown rules simply do not run."""
    result = _evaluate(_episode(), _profile(enabled_rules=["duration", "no_such_rule"]))
    assert result.passed is True


@pytest.mark.unit
def test_record_persists_the_result_with_the_profile_identity() -> None:
    catalog = _Catalog()
    service = ValidationService(catalog)

    result = service.record("episode-1", _episode(), _profile(required_channels=["gripper"]))

    assert result.passed is False
    assert len(catalog.recorded) == 1
    saved = catalog.recorded[0]
    assert saved["episode_id"] == "episode-1"
    assert saved["profile_hash"] == profile_hash(_profile(required_channels=["gripper"]))
    assert saved["profile_name"] == "manipulation"
    assert "VALIDATION_FAILED" in saved["reason_codes"]


@pytest.mark.unit
def test_validating_a_stored_episode_never_opens_the_file() -> None:
    """FR-003: re-validation after a profile fix must not re-ingest bytes."""
    stored = _episode()
    catalog = _Catalog()
    catalog.episodes["episode-1"] = {"id": "episode-1", "metadata": stored.metadata()}
    service = ValidationService(catalog)

    result = service.validate_stored_episode("episode-1", _profile(required_channels=["action"]))

    assert result.passed is True
    assert result.profile_name == "manipulation"
    assert catalog.recorded[0]["episode_id"] == "episode-1"


@pytest.mark.unit
def test_revalidating_under_a_second_profile_keeps_both_results() -> None:
    """FR-003: a new profile is a new statement, not an overwrite."""
    catalog = _Catalog()
    catalog.episodes["episode-1"] = {"id": "episode-1", "metadata": _episode().metadata()}
    service = ValidationService(catalog)

    service.validate_stored_episode("episode-1", _profile(required_channels=["missing"]))
    service.validate_stored_episode("episode-1", _profile(required_channels=["action"]))

    assert [row["passed"] for row in catalog.recorded] == [False, True]
    assert catalog.recorded[0]["profile_hash"] != catalog.recorded[1]["profile_hash"]


@pytest.mark.unit
def test_validating_an_unknown_episode_raises() -> None:
    with pytest.raises(KeyError):
        ValidationService(_Catalog()).validate_stored_episode("nope", _profile())


@pytest.mark.unit
def test_stored_metadata_round_trips_into_an_extraction() -> None:
    restored = extraction_from_metadata(_episode().metadata())

    assert restored.frame_count == 300
    assert restored.robot_type == "so100_follower"
    assert restored.fps == 30.0
    assert {c.name for c in restored.channels} == {"action", "observation.state"}


@pytest.mark.unit
def test_degraded_metadata_validates_as_a_failure_rather_than_crashing() -> None:
    """An episode with no readable frame count should fail validation, not raise."""
    restored = extraction_from_metadata({})

    result = _evaluate(restored, _profile())

    assert result.passed is False
    assert ViolationCode.EMPTY_EPISODE.value in result.reason_codes
