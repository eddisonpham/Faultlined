"""Integration tests for curated slices and the failures read view (real SQL).

These drive the actual repository against the dedicated test database, so the
predicates, joins, and the membership recompute are exercised for real rather than
through a stub. They build their own world with UUID-scoped identity, so they do not
depend on — or disturb — anything another test wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog, SliceNameConflict
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn


@pytest.fixture
def catalog() -> Iterator[PostgresCatalog]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    settings = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(settings)
    yield PostgresCatalog(settings)


def _seed(catalog: PostgresCatalog, *, verdict: str, frames: int, passed: bool) -> dict[str, Any]:
    """Register one episode with quality, and optionally a validation verdict."""
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job("ingest", {}, f"curation-{token}", "test-correlation")
    episode = catalog.register_episode(
        source_hash=token,
        artifact_hash=token,
        size_bytes=1024,
        metadata={"task": "pick", "robot": "so101"},
        job_id=str(job["id"]),
        episode_key=f"episode_{token[:8]}",
        episode_format="lerobot-v3",
    )
    catalog.record_episode_quality(
        str(episode["id"]),
        {
            "frame_count": frames,
            "movement_score": 0.04,
            "jerk_score": 0.5 if verdict == "jerky" else 0.01,
            "stall_ratio": 0.9 if verdict == "stalled" else 0.05,
            "verdict": verdict,
            "dims": [],
        },
    )
    if passed:
        # An episode is only `valid` once a profile passes it; ingest alone leaves it
        # `ingested`, so a "survivor" has to actually clear validation.
        catalog.record_validation(
            episode_id=str(episode["id"]),
            profile_hash=f"h-{token[:16]}",
            profile_name=f"ok-{token[:8]}",
            profile_version="1",
            passed=True,
            reason_codes=[],
            violations=[],
        )
    else:
        catalog.record_validation(
            episode_id=str(episode["id"]),
            profile_hash=f"h-{token[:16]}",
            profile_name=f"strict-{token[:8]}",
            profile_version="1",
            passed=False,
            reason_codes=["TOO_FEW_FRAMES"],
            violations=[{"code": "TOO_FEW_FRAMES", "message": "expected more frames"}],
        )
    return episode


@pytest.mark.integration
def test_failures_summary_counts_quarantined_episodes_by_reason_code(
    catalog: PostgresCatalog,
) -> None:
    _seed(catalog, verdict="smooth", frames=300, passed=False)
    _seed(catalog, verdict="smooth", frames=300, passed=False)

    summary = catalog.failure_summary()
    assert summary is not None
    assert summary["quarantined_count"] >= 2
    assert summary["reason_codes"]["TOO_FEW_FRAMES"] >= 2
    assert summary["episodes_evaluated"] >= 2
    assert summary["by_profile"]
    assert any(row["format"] == "lerobot-v3" for row in summary["by_format"])


@pytest.mark.integration
def test_failing_episodes_joins_quality_and_filters_by_reason_code(
    catalog: PostgresCatalog,
) -> None:
    episode = _seed(catalog, verdict="jerky", frames=5, passed=False)

    rows = catalog.failing_episodes(limit=500)
    mine = [row for row in rows if row["id"] == episode["id"]]
    assert len(mine) == 1
    assert mine[0]["state"] == "quarantined"
    assert mine[0]["reason_codes"] == ["TOO_FEW_FRAMES"]
    assert mine[0]["profile_name"].startswith("strict-")
    assert mine[0]["verdict"] == "jerky"
    assert mine[0]["frame_count"] == 5

    assert mine[0] in catalog.failing_episodes(limit=500, reason_code="TOO_FEW_FRAMES")
    assert mine[0] not in catalog.failing_episodes(limit=500, reason_code="VALIDATION_FAILED")


@pytest.mark.integration
def test_a_slice_recomputes_membership_and_exports_identity(
    catalog: PostgresCatalog,
) -> None:
    good = _seed(catalog, verdict="smooth", frames=300, passed=True)
    saved = catalog.register_slice(
        name=f"survivors-{uuid.uuid4().hex[:8]}",
        notes="passed curation",
        filter_config={"state": "valid", "flag": ""},
    )
    slice_id = str(saved["id"])

    manifest = catalog.slice_manifest(slice_id, limit=500)
    assert manifest is not None
    assert manifest["slice_id"] == slice_id
    # `count` must describe the page it ships with, not a stale or hardcoded zero.
    assert manifest["count"] == len(manifest["items"])
    ids = {item["id"] for item in manifest["items"]}
    assert str(good["id"]) in ids
    # Content identity is what a downstream dataset build needs to read the bytes.
    assert all(item["artifact_hash"] and item["source_hash"] for item in manifest["items"])

    detail = catalog.get_slice(slice_id)
    assert detail is not None
    assert detail["filter_config"] == {"state": "valid", "flag": ""}
    assert detail["member_count"] >= 1

    # Quarantining a member changes the next manifest read: membership is not a snapshot.
    catalog.record_validation(
        episode_id=str(good["id"]),
        profile_hash=f"h-{uuid.uuid4().hex[:12]}",
        profile_name="late-profile",
        profile_version="1",
        passed=False,
        reason_codes=["VALIDATION_FAILED"],
        violations=[],
    )
    after = catalog.slice_manifest(slice_id, limit=500)
    assert after is not None
    assert str(good["id"]) not in {item["id"] for item in after["items"]}

    # Fetch the slice rather than scanning the capped slice list: this database is
    # shared with the rest of the suite and that page keeps growing, so membership
    # in it says nothing about this slice.
    assert catalog.get_slice(slice_id) is not None
    updated = catalog.update_slice(slice_id, notes="retuned", filter_config={"state": "valid"})
    assert updated is not None
    assert updated["notes"] == "retuned"
    assert catalog.delete_slice(slice_id) is True
    assert catalog.get_slice(slice_id) is None
    assert catalog.slice_manifest(slice_id) is None


@pytest.mark.integration
def test_duplicate_slice_names_conflict_and_unknown_ids_are_empty(
    catalog: PostgresCatalog,
) -> None:
    name = f"unique-{uuid.uuid4().hex[:8]}"
    catalog.register_slice(name=name, filter_config={"state": "valid"})
    with pytest.raises(SliceNameConflict):
        catalog.register_slice(name=name, filter_config={})

    assert catalog.get_slice("no-such-slice") is None
    assert catalog.delete_slice("no-such-slice") is False
    assert catalog.update_slice("no-such-slice", notes="x") is None


@pytest.mark.integration
def test_a_slice_with_an_unknown_filter_state_has_no_manifest(
    catalog: PostgresCatalog,
) -> None:
    saved = catalog.register_slice(
        name=f"bad-{uuid.uuid4().hex[:8]}", filter_config={"state": "not-a-state"}
    )
    # An unreadable filter must not silently widen the slice to "everything".
    assert catalog.slice_manifest(str(saved["id"])) is None


@pytest.mark.integration
def test_the_motion_trace_round_trips_through_real_sql(catalog: PostgresCatalog) -> None:
    """The trace is stored verbatim: runs of [seconds, score], holes intact."""
    episode = _seed(catalog, verdict="smooth", frames=6, passed=True)
    trace = [[[1.0, 0.2], [2.0, 0.3]], [[31.0, 0.4], [32.0, 0.5]]]
    catalog.record_episode_quality(
        str(episode["id"]),
        {
            "frame_count": 6,
            "movement_score": 0.04,
            "jerk_score": 0.01,
            "stall_ratio": 0.1,
            "verdict": "smooth",
            "dims": [],
            "motion_trace": trace,
        },
    )
    stored = catalog.get_episode_quality(str(episode["id"]))
    assert stored is not None
    assert stored["motion_trace"] == trace


@pytest.mark.integration
def test_slice_impact_names_what_it_drops_and_why(catalog: PostgresCatalog) -> None:
    """Kept/dropped partition the dataset, and every drop names its reason.

    Counts are asserted as deltas because the test database is shared: the
    before/after difference of my own three episodes is exact no matter what
    other tests wrote.
    """
    saved = catalog.register_slice(
        name=f"impact-jerky-{uuid.uuid4().hex[:8]}",
        filter_config={"state": "valid", "flag": "jerky"},
    )
    slice_id = str(saved["id"])
    before = catalog.slice_impact(slice_id)
    assert before is not None

    _seed(catalog, verdict="jerky", frames=300, passed=True)  # kept
    _seed(catalog, verdict="smooth", frames=300, passed=True)  # drops: verdict=smooth
    _seed(catalog, verdict="jerky", frames=300, passed=False)  # drops: state=quarantined

    impact = catalog.slice_impact(slice_id)
    assert impact is not None
    assert impact["filters"] == {"state": "valid", "flag": "jerky"}
    assert not impact["reorders_only"]
    assert impact["kept"]["count"] - before["kept"]["count"] == 1
    assert impact["dropped"]["count"] - before["dropped"]["count"] == 2
    # kept and dropped partition the dataset; no episode is unaccounted for.
    assert impact["dataset"]["episodes"] == impact["kept"]["count"] + impact["dropped"]["count"]

    def reasons(table: dict[str, Any]) -> dict[str, int]:
        return {row["reason"]: row["count"] for row in table["drop_reasons"]}

    after, start = reasons(impact), reasons(before)
    # State is attributed before flag: the quarantined jerky episode fails state first.
    assert after.get("state=quarantined", 0) - start.get("state=quarantined", 0) == 1
    assert after.get("verdict=smooth", 0) - start.get("verdict=smooth", 0) == 1
    assert sum(after.values()) == impact["dropped"]["count"]

    # The kept side is smoother than what it dropped: the whole point of the filter.
    kept_jerk = impact["kept"]["median_jerk_score"]
    dropped_jerk = impact["dropped"]["median_jerk_score"]
    assert kept_jerk is not None and dropped_jerk is not None
    assert kept_jerk >= dropped_jerk


@pytest.mark.integration
def test_an_ordering_slice_drops_nothing_and_says_so(catalog: PostgresCatalog) -> None:
    saved = catalog.register_slice(
        name=f"impact-long-{uuid.uuid4().hex[:8]}", filter_config={"flag": "long"}
    )
    impact = catalog.slice_impact(str(saved["id"]))
    assert impact is not None
    assert impact["reorders_only"]
    assert impact["drop_reasons"] == []
    assert impact["dropped"]["count"] == 0
    assert impact["kept"]["count"] == impact["dataset"]["episodes"]
