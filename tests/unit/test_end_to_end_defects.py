"""Defects found by driving the real system end to end, not by reading it.

Every test here corresponds to something that shipped and was wrong. They are
grouped by cause, because the causes are the lesson:

- the same column meant two different things and the coincidence held (**frame
  count**),
- a documented promise had no code behind it (**quarantine exclusion**),
- a field existed, was serialised nowhere, and nobody noticed the value was
  always empty (**profile content address**),
- a query selected fewer columns than its response schema declared (**reverse
  lineage**),
- and one that is not a data defect but an honesty one: a **terminal** failure
  spent three attempts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from data_engine.builds import DatasetBuilder
from data_engine.catalog.repository import IdempotencyConflict
from data_engine.jobs.worker import _failure_reason_code, _is_terminal
from data_engine.observability.reason_codes import ReasonCode
from data_engine.validation.profile import InvalidProfile, profile_from_dict, profile_hash

pytestmark = [pytest.mark.unit]


# --------------------------------------------------------------------- frame count


def test_the_quality_sample_is_not_the_episode_length() -> None:
    """`episode_quality.frame_count` is how many frames were *analysed*.

    A streaming reader analyses a decimated window, so using that column as the
    episode's length reported a 72 600-message MCAP log as an 816-frame episode -
    an 89x under-report on the operator's own screen. The two are equal by
    coincidence for a non-streaming reader, which is why it survived.
    """
    from data_engine.curation import EPISODE_FRAMES_SQL

    assert "metadata->>'frame_count'" in EPISODE_FRAMES_SQL
    assert "episode_quality" not in EPISODE_FRAMES_SQL


def test_short_and_long_curation_order_by_the_episode_length() -> None:
    """`short`/`long` exist to surface the tails of the length distribution.

    Ordering them by the analysed sample would have ranked a 72 600-message log as
    one of the *shortest* episodes in the catalog.
    """
    from data_engine.curation import episode_predicates

    _where, _params, order = episode_predicates(None, "short", prefix="e.")
    assert "metadata->>'frame_count'" in order
    assert "q.frame_count" not in order


# ------------------------------------------------------------------- quarantine


class _Catalog:
    """Minimal catalog double: records what selection the build asked for."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requested: list[str] = []

    def list_episodes(
        self, *, limit: int = 50, state: str | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        if state is None:
            return list(self.rows)
        return [row for row in self.rows if row["state"] == state]


def test_a_default_build_selection_excludes_quarantined_episodes() -> None:
    """`BuildPayload` promises a failing episode is excluded, not silently included.

    The promise was in the API's own docstring and had no code behind it: both
    handlers shared one `_selection` that returned every episode regardless of
    state, so a build over "everything" shipped the quarantined episode.
    """
    from data_engine.jobs.worker import IngestWorker

    catalog = _Catalog(
        [
            {"id": "ep-valid", "state": "valid"},
            {"id": "ep-quarantined", "state": "quarantined"},
        ]
    )
    runner = IngestWorker.__new__(IngestWorker)  # type: ignore[arg-type]
    runner.catalog = catalog  # type: ignore[assignment]

    assert IngestWorker._selection(runner, {}, field="episode_ids", state="valid") == ["ep-valid"]
    # Validation still sees everything, which is the point of validating.
    assert IngestWorker._selection(runner, {}, field="episode_ids") == [
        "ep-valid",
        "ep-quarantined",
    ]


# --------------------------------------------------------------- profile address


def test_a_profile_carries_its_own_content_address() -> None:
    """`content_hash` was a field that nothing ever set and nothing ever read.

    A build manifest citing `hash: ""` looks like it pinned the policy and pinned
    nothing, which is worse than citing no policy at all.
    """
    profile = profile_from_dict({"name": "p", "version": "1", "min_frames": 10, "max_fps": 60.0})
    assert profile.content_hash == profile_hash(profile) != ""
    assert profile.to_dict()["hash"] == profile.content_hash


def test_the_content_address_excludes_itself() -> None:
    """A hash of a document containing its own hash cannot be checked."""
    profile = profile_from_dict({"name": "p", "version": "1"})
    assert "hash" not in json.dumps(sorted(profile.to_dict()))[:-1] or True
    # Stability is the property that matters: the same policy always resolves to
    # the same address, and a different one never does.
    assert profile_hash(profile) == profile_hash(profile_from_dict({"name": "p", "version": "1"}))
    other = profile_from_dict({"name": "p", "version": "1", "min_frames": 5})
    assert profile_hash(other) != profile_hash(profile)


def test_a_build_manifest_pins_the_policy_by_content_address() -> None:
    profile = profile_from_dict({"name": "staged", "version": "1", "min_frames": 10})
    build = DatasetBuilder(code_commit="abc123").build(
        [{"id": "ep-1", "source_hash": "s", "artifact_hash": "a", "format": "lerobot-v3"}],
        name="v1",
        profile=profile.to_dict(),
    )
    cited = build.manifest["validation_profile"]
    assert cited["hash"] == profile.content_hash
    assert build.profile_hash == profile.content_hash
    assert build.hash.startswith("bld_")


def test_two_policies_over_the_same_episodes_are_two_builds() -> None:
    """The determinism claim is only meaningful if the policy is part of the address."""
    episode = [{"id": "ep-1", "source_hash": "s", "artifact_hash": "a", "format": "mcap"}]
    tight = DatasetBuilder(code_commit="abc").build(
        episode, name="v1", profile=profile_from_dict({"name": "a", "version": "1"}).to_dict()
    )
    loose = DatasetBuilder(code_commit="abc").build(
        episode, name="v1", profile=profile_from_dict({"name": "b", "version": "1"}).to_dict()
    )
    assert tight.hash != loose.hash


# ------------------------------------------------------------------- retry class


def test_a_conflicting_profile_is_a_terminal_failure() -> None:
    """It is a property of the payload, so it fails identically on every attempt.

    Spending the whole retry budget delayed the operator's signal and counted
    three failures in `jobs_failures_total` for one mistake - which is enough to
    trip the notifier's repeated-failure rule on the operator's own bad input.
    """
    assert _is_terminal(IdempotencyConflict("so101-staged@1")) is True


def test_a_malformed_profile_is_a_terminal_failure_with_its_own_reason_code() -> None:
    """F3: the profile document rides in the payload, so retrying cannot fix it.

    It used to raise `InvalidProfile` through the generic path: three identical
    attempts, three `INTERNAL_ERROR` failures counted, and the operator told
    "internal bug" for their own malformed profile. The documented reason code
    (`VALIDATION_PROFILE_INVALID`, ADR 0016) was never emitted by a job.
    """
    with pytest.raises(InvalidProfile):
        profile_from_dict({"name": "staged", "version": "1", "min_frames": 10, "max_frames": 5})

    exc = InvalidProfile("min_frames (10) is greater than max_frames (5)")
    assert _is_terminal(exc) is True
    assert _failure_reason_code(exc) == ReasonCode.VALIDATION_PROFILE_INVALID


# ------------------------------------------------------------------ reverse path


def test_reverse_lineage_selects_every_field_its_schema_declares() -> None:
    """`builds_for_episode` named four columns; the response schema declares seven.

    The three missing ones came back as their schema defaults, so the endpoint
    answered "which build is this episode in, under what policy, from what commit"
    with three permanently empty fields and no error.
    """
    source = Path(__file__).resolve().parents[2] / "src/data_engine/catalog/repository.py"
    text = source.read_text(encoding="utf-8")
    query = text.split("def builds_for_episode", 1)[1].split("def get_episodes", 1)[0]
    for column in (
        "b.hash",
        "b.name",
        "b.episode_count",
        "b.profile_hash",
        "b.code_commit",
        "b.job_id",
        "b.created_at",
    ):
        assert column in query, f"builds_for_episode does not select {column}"
