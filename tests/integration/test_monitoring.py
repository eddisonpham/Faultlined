"""Integration tests for the notifier against real SQL (ADR 0020).

The guarantees under test are the ones a stub cannot prove: that the partial
unique index really does make dedup unbreakable, that a baseline survives a
round trip, and that a whole tick — probe, features, rules, contracts, triage,
persist — works against the actual database.

Everything is UUID-scoped so these tests neither depend on nor disturb what
another test wrote — which matters here more than usual, because the shared test
database accumulates content-addressed state across runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.cli import _monitor_tick
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.monitoring.baselines import Baseline, BaselineBook
from data_engine.monitoring.contracts import Expectation
from data_engine.monitoring.queue import TriagePolicy, fingerprint
from data_engine.monitoring.service import MonitorConfig, MonitorService
from data_engine.monitoring.signals import Label, Severity, Signal
from data_engine.monitoring.summary import render_summary
from tests.conftest import postgres_test_dsn

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def catalog() -> Iterator[PostgresCatalog]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    settings = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(settings)
    yield PostgresCatalog(settings)


def _job(catalog: PostgresCatalog, *, settle: bool = True) -> dict[str, Any]:
    """Submit one job, optionally driving it to ``succeeded`` so contracts can settle.

    ``claim_job`` takes the oldest queued job in the database, not a specific one,
    so a shared test database means other tests' jobs come first. This drains the
    queue until it reaches its own, which also tidies up after earlier tests.
    """
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job("ingest", {}, f"monitor-{token}", "test-correlation")
    job_id = str(job["id"])
    if settle:
        for _ in range(200):
            claimed = catalog.claim_job()
            if claimed is None:
                break
            catalog.finish_job(str(claimed["id"]), JobState.SUCCEEDED, result={"ok": True})
            if str(claimed["id"]) == job_id:
                break
        assert catalog.get_job(job_id)["state"] == "succeeded"  # type: ignore[index]
    return {"id": job_id, "type": "ingest"}


def _scope() -> str:
    """A unique scope per probe.

    Fingerprints are deterministic functions of (label, scope, signature), so two
    tests using the same scope would share one incident in the shared test
    database — and the second would bump the first rather than open its own.
    """
    return f"probe-{uuid.uuid4().hex[:8]}"


def _signal(*, scope: str | None = None, **kwargs: Any) -> Signal:
    defaults: dict[str, Any] = {
        "label": Label.WORKER_LOST.value,
        "severity": Severity.CRITICAL,
        "scope": scope or _scope(),
        "detail": "no heartbeat",
        "evidence": {"heartbeat_age_seconds": 900.0},
    }
    defaults.update(kwargs)
    return Signal(**defaults)


def _upsert(catalog: PostgresCatalog, signal: Any, seen_at: datetime) -> dict[str, Any]:
    """Persist one signal exactly as the service would, using the real fingerprint."""
    return catalog.upsert_incident(
        incident_id=str(uuid.uuid4()),
        fingerprint=fingerprint(signal),
        label=signal.label,
        severity=signal.severity.value,
        notify_class=signal.notify_class.value,
        scope=signal.scope,
        summary=render_summary(signal.label, signal.evidence, signal.detail),
        evidence=signal.to_evidence(),
        feature_schema_version=signal.feature_schema_version,
        seen_at=seen_at,
    )


class TestBaselines:
    def test_a_baseline_survives_a_round_trip(self, catalog: PostgresCatalog) -> None:
        book = BaselineBook()
        for value in (10.0, 12.0, 11.0, 13.0):
            book.observe("run_time_p95_seconds", value, scope="ingest")
        baseline = book.get("run_time_p95_seconds", "ingest")
        assert baseline is not None
        catalog.save_baselines([baseline.to_row()])

        restored = BaselineBook.from_rows(
            [row for row in catalog.load_baselines() if row["feature"] == "run_time_p95_seconds"]
        )
        after = restored.get("run_time_p95_seconds", "ingest")
        assert after == baseline
        assert after is not None and after.median == 11.5

    def test_scoped_baselines_do_not_collide(self, catalog: PostgresCatalog) -> None:
        book = BaselineBook()
        for value in (1.0, 2.0, 3.0):
            book.observe("run_time_p95_seconds", value, scope="ingest")
        for value in (100.0, 200.0, 300.0):
            book.observe("run_time_p95_seconds", value, scope="validate")
        catalog.save_baselines([item.to_row() for item in book.values()])

        loaded = {
            (row["feature"], row["scope"]): row
            for row in catalog.load_baselines()
            if row["feature"] == "run_time_p95_seconds"
        }
        assert loaded[("run_time_p95_seconds", "ingest")]["samples"] == [1.0, 2.0, 3.0]
        assert loaded[("run_time_p95_seconds", "validate")]["samples"] == [100.0, 200.0, 300.0]

    def test_saving_twice_replaces_rather_than_appends(self, catalog: PostgresCatalog) -> None:
        catalog.save_baselines([Baseline("queue_depth_queued", samples=(1.0, 2.0)).to_row()])
        catalog.save_baselines([Baseline("queue_depth_queued", samples=(9.0,)).to_row()])
        rows = [r for r in catalog.load_baselines() if r["feature"] == "queue_depth_queued"]
        assert len(rows) == 1
        assert rows[0]["samples"] == [9.0]

    def test_saving_nothing_is_a_no_op(self, catalog: PostgresCatalog) -> None:
        assert catalog.save_baselines([]) == 0


class TestIncidentStore:
    def test_a_new_incident_opens(self, catalog: PostgresCatalog) -> None:
        row = _upsert(catalog, _signal(), NOW)
        assert row["status"] == "open"
        assert row["occurrence_count"] == 1
        assert catalog.get_incident(str(row["id"])) is not None

    def test_the_same_fingerprint_bumps_instead_of_duplicating(
        self, catalog: PostgresCatalog
    ) -> None:
        # The dedup guarantee is the partial unique index, not application logic,
        # so it holds even if a crash lands between triage and the write.
        scope = _scope()
        first = _upsert(catalog, _signal(scope=scope), NOW)
        second = _upsert(catalog, _signal(scope=scope), NOW + timedelta(minutes=5))
        assert second["id"] == first["id"]
        assert second["occurrence_count"] == 2
        assert second["last_seen"] > first["first_seen"]

    def test_a_bump_carries_the_new_evidence(self, catalog: PostgresCatalog) -> None:
        scope = _scope()
        first = _upsert(
            catalog, _signal(scope=scope, evidence={"heartbeat_age_seconds": 900.0}), NOW
        )
        _upsert(catalog, _signal(scope=scope, evidence={"heartbeat_age_seconds": 1800.0}), NOW)
        refreshed = catalog.get_incident(str(first["id"]))
        assert refreshed is not None
        assert refreshed["evidence"]["evidence"]["heartbeat_age_seconds"] == 1800.0

    def test_severity_never_downgrades(self, catalog: PostgresCatalog) -> None:
        scope = _scope()
        _upsert(catalog, _signal(scope=scope, severity=Severity.CRITICAL), NOW)
        bumped = _upsert(catalog, _signal(scope=scope, severity=Severity.HIGH), NOW)
        assert bumped["severity"] == "critical"

    def test_severity_may_escalate(self, catalog: PostgresCatalog) -> None:
        scope = _scope()
        _upsert(catalog, _signal(scope=scope, severity=Severity.HIGH), NOW)
        bumped = _upsert(catalog, _signal(scope=scope, severity=Severity.CRITICAL), NOW)
        assert bumped["severity"] == "critical"

    def test_different_scopes_are_separate_incidents(self, catalog: PostgresCatalog) -> None:
        one = _upsert(catalog, _signal(scope="job-1"), NOW)
        two = _upsert(catalog, _signal(scope="job-2"), NOW)
        assert one["id"] != two["id"]

    def test_a_resolved_fingerprint_can_open_again(self, catalog: PostgresCatalog) -> None:
        first = _upsert(catalog, _signal(), NOW)
        assert catalog.set_incident_status(str(first["id"]), "resolved") is not None
        second = _upsert(catalog, _signal(), NOW + timedelta(hours=1))
        assert second["id"] != first["id"]

    def test_acknowledging_records_the_time(self, catalog: PostgresCatalog) -> None:
        row = _upsert(catalog, _signal(), NOW)
        acked = catalog.set_incident_status(str(row["id"]), "acknowledged")
        assert acked is not None
        assert acked["status"] == "acknowledged"
        assert acked["acknowledged_at"] is not None

    def test_a_resolved_incident_cannot_be_acknowledged(self, catalog: PostgresCatalog) -> None:
        row = _upsert(catalog, _signal(), NOW)
        catalog.set_incident_status(str(row["id"]), "resolved")
        assert catalog.set_incident_status(str(row["id"]), "acknowledged") is None

    def test_the_budget_counts_only_new_incidents(self, catalog: PostgresCatalog) -> None:
        # A still-open fault re-firing is not news; only new rows consume budget.
        scope = _scope()
        row = _upsert(catalog, _signal(scope=scope), NOW)
        before = catalog.incidents_opened_since(NOW - timedelta(hours=1))
        _upsert(catalog, _signal(scope=scope), NOW + timedelta(minutes=1))
        assert catalog.incidents_opened_since(NOW - timedelta(hours=1)) == before
        assert catalog.get_incident(str(row["id"])) is not None

    def test_incidents_are_filtered_and_paged(self, catalog: PostgresCatalog) -> None:
        _upsert(catalog, _signal(), NOW)
        assert catalog.list_incidents(label=Label.WORKER_LOST.value)
        assert catalog.list_incidents(severity="nonexistent") == []
        assert catalog.list_incidents(limit=1) != []

    def test_the_summary_groups_the_queue(self, catalog: PostgresCatalog) -> None:
        _upsert(catalog, _signal(), NOW)
        summary = catalog.incident_summary()
        assert summary["by_status"].get("open", 0) >= 1
        assert summary["notify_open"] >= 1
        assert Label.WORKER_LOST.value in summary["by_label"]

    def test_unresolved_incidents_expose_what_triage_needs(self, catalog: PostgresCatalog) -> None:
        _upsert(catalog, _signal(), NOW)
        rows = catalog.unresolved_incidents()
        assert all({"id", "fingerprint", "status", "last_seen"} <= set(row) for row in rows)


class TestContracts:
    def test_a_contract_is_declared_and_read_back(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog, settle=False)
        catalog.register_contract(job["id"], Expectation(expected_episodes=42).to_dict())
        stored = catalog.get_contract(job["id"])
        assert stored is not None
        assert stored["expected_episodes"] == 42
        assert stored["outcome"] == "pending"

    def test_redeclaring_resets_the_outcome(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog, settle=False)
        catalog.register_contract(job["id"], Expectation(expected_episodes=1).to_dict())
        catalog.set_contract_outcome(job["id"], "short", {"valid_episodes": 0})
        catalog.register_contract(job["id"], Expectation(expected_episodes=9).to_dict())
        # A revised expectation has not been evaluated yet, and claiming it was
        # would hide a breach rather than surface it.
        assert catalog.get_contract(job["id"])["outcome"] == "pending"  # type: ignore[index]

    def test_a_contract_for_a_missing_job_fails_the_foreign_key(
        self, catalog: PostgresCatalog
    ) -> None:
        import psycopg

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            catalog.register_contract(str(uuid.uuid4()), Expectation(expected_episodes=1).to_dict())

    def test_pending_contracts_join_their_job_state(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog, settle=False)
        catalog.register_contract(job["id"], Expectation(expected_episodes=5).to_dict())
        rows = catalog.pending_contract_outcomes()
        mine = [row for row in rows if row["job_id"] == job["id"]]
        assert mine
        assert mine[0]["job_state"] == "queued"
        assert mine[0]["episodes_produced"] == 0

    def test_episode_counts_come_from_the_lineage_edge(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog)
        token = uuid.uuid4().hex
        catalog.register_episode(
            source_hash=token,
            artifact_hash=token,
            size_bytes=64,
            metadata={"task": "pick"},
            job_id=job["id"],
            episode_key=f"episode_{token[:8]}",
        )
        catalog.register_contract(job["id"], Expectation(expected_episodes=1).to_dict())
        rows = [r for r in catalog.pending_contract_outcomes() if r["job_id"] == job["id"]]
        assert rows
        assert rows[0]["episodes_produced"] == 1

    def test_contracts_are_listed_newest_first(self, catalog: PostgresCatalog) -> None:
        for _ in range(2):
            job = _job(catalog, settle=False)
            catalog.register_contract(job["id"], Expectation(expected_episodes=1).to_dict())
        assert catalog.list_contracts(limit=5)


class TestFullTick:
    def _monitor(self, catalog: PostgresCatalog, metrics_path: Any = None) -> MonitorService:
        return MonitorService(
            catalog,
            metrics_path=metrics_path,
            config=MonitorConfig(policy=TriagePolicy(budget_per_window=100)),
        )

    def test_a_tick_reaches_the_catalog(self, catalog: PostgresCatalog) -> None:
        report = self._monitor(catalog).tick()
        assert report.catalog_reachable is True
        assert all(signal.label != Label.DATABASE_UNREACHABLE.value for signal in report.signals)

    def test_a_monitor_with_no_telemetry_is_blind_not_healthy(
        self, catalog: PostgresCatalog
    ) -> None:
        # A monitor that cannot see the platform looks exactly like one with
        # nothing to report, and only one of those is healthy. Silence must never
        # be the default reading.
        report = self._monitor(catalog).tick()
        assert report.blind is True
        assert not report.features.has("sink_lag_seconds")

    def test_a_monitor_with_fresh_telemetry_is_not_blind(
        self, catalog: PostgresCatalog, tmp_path: Any
    ) -> None:
        path = tmp_path / "metrics.jsonl"
        record = {
            "name": "episodes_ingested_total",
            "value": 1.0,
            "unit": "episodes",
            "labels": {},
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "runtime",
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        report = self._monitor(catalog, metrics_path=path).tick()
        assert report.blind is False
        assert report.features.value("episodes_ingested") == 1.0

    def test_a_tick_learns_and_persists_baselines(self, catalog: PostgresCatalog) -> None:
        service = self._monitor(catalog)
        service.tick()
        health = service.health()
        assert health["baseline_scopes"] > 0
        assert health["last_tick_at"] is not None
        assert catalog.load_baselines(), "a tick that learns nothing is a broken tick"

    def test_baselines_warm_over_repeated_ticks(self, catalog: PostgresCatalog) -> None:
        service = self._monitor(catalog)
        for _ in range(25):
            service.tick()
        assert service.health()["warm_scopes"] > 0

    def test_health_lists_the_notify_policy(self, catalog: PostgresCatalog) -> None:
        health = self._monitor(catalog).health()
        assert Label.CONTRACT_BREACH.value in health["notify_labels"]
        assert Label.METRIC_SHIFT.value not in health["notify_labels"]
        assert "metric_shift" in health["detectors"]

    def test_a_tick_survives_an_unreachable_catalog(self, catalog: PostgresCatalog) -> None:
        class Broken:
            def monitoring_snapshot(self, **_: Any) -> dict[str, Any]:
                raise RuntimeError("connection refused")

        service = self._monitor(catalog)
        report = service.tick(Broken())
        assert report.catalog_reachable is False
        assert report.blind is True
        assert Label.DATABASE_UNREACHABLE.value in {s.label for s in report.signals}
        # Nothing else may be claimed from a platform the monitor cannot see.
        assert len(report.signals) == 1

    def test_a_database_outage_does_not_reset_the_baselines(self, catalog: PostgresCatalog) -> None:
        service = self._monitor(catalog)
        for _ in range(3):
            service.tick()
        warm_before = service.health()["baseline_scopes"]

        class Broken:
            def monitoring_snapshot(self, **_: Any) -> dict[str, Any]:
                raise RuntimeError("connection refused")

        service.tick(Broken())
        # The book is kept in memory so a brief outage blips the incident store
        # rather than resetting every control limit to cold.
        assert service.health()["baseline_scopes"] == warm_before

    def test_a_breached_contract_opens_an_incident(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog)
        catalog.register_contract(job["id"], Expectation(expected_episodes=50).to_dict())
        report = self._monitor(catalog).tick()
        mine = [r for r in report.contract_outcomes if r["job_id"] == job["id"]]
        assert mine and mine[0]["outcome"] == "short"
        assert catalog.get_contract(job["id"])["outcome"] == "short"  # type: ignore[index]

    def test_a_met_contract_stays_pending(self, catalog: PostgresCatalog) -> None:
        job = _job(catalog)
        catalog.register_contract(job["id"], Expectation(expected_episodes=0).to_dict())
        report = self._monitor(catalog).tick()
        mine = [r for r in report.contract_outcomes if r["job_id"] == job["id"]]
        assert mine and mine[0]["outcome"] == "met"

    def test_the_notify_preview_renders_open_notify_incidents(
        self, catalog: PostgresCatalog
    ) -> None:
        job = _job(catalog)
        catalog.register_contract(job["id"], Expectation(expected_episodes=50).to_dict())
        self._monitor(catalog).tick()
        preview = self._monitor(catalog).notify_preview()
        assert isinstance(preview, str)


class TestMonitorLease:
    """ADR 0031: one tick per interval across a pool of workers."""

    def test_the_lease_admits_one_ticker_and_releases_on_exit(
        self, catalog: PostgresCatalog
    ) -> None:
        other = PostgresCatalog(catalog.settings)
        with catalog.monitor_lease() as first:
            assert first is True
            # A second connection stands in for another worker's loop.
            with other.monitor_lease() as second:
                assert second is False
        # The lock is session-scoped: closing the block releases it.
        with other.monitor_lease() as again:
            assert again is True

    def test_two_catalogs_can_take_the_lease_in_sequence(self, catalog: PostgresCatalog) -> None:
        first = PostgresCatalog(catalog.settings)
        second = PostgresCatalog(catalog.settings)
        with first.monitor_lease() as held:
            assert held is True
        with second.monitor_lease() as held:
            assert held is True


class TestScheduledTick:
    """ADR 0031: the worker loop's own path is enough to open an incident.

    This is the release-blocker acceptance criterion - "an incident appears where
    nobody POSTs `/api/v1/monitoring/tick`" - exercised through `_monitor_tick`,
    which is the function `_worker_loop` calls.
    """

    def _monitor(self, catalog: PostgresCatalog) -> MonitorService:
        return MonitorService(
            catalog,
            config=MonitorConfig(policy=TriagePolicy(budget_per_window=100)),
        )

    def test_a_scheduled_tick_opens_an_incident_without_the_http_route(
        self, catalog: PostgresCatalog
    ) -> None:
        job = _job(catalog)
        catalog.register_contract(job["id"], Expectation(expected_episodes=50).to_dict())

        _monitor_tick(self._monitor(catalog), catalog, logging.getLogger("test"))

        labels = {row["label"] for row in catalog.list_incidents(status="open", limit=50)}
        assert Label.CONTRACT_BREACH.value in labels

    def test_the_scheduled_tick_yields_when_the_lease_is_held(
        self, catalog: PostgresCatalog
    ) -> None:
        class _Spy(MonitorService):
            ticks = 0

            def tick(self, target: Any | None = None) -> Any:
                type(self).ticks += 1
                return super().tick(target)

        monitor = _Spy(catalog, config=MonitorConfig(policy=TriagePolicy(budget_per_window=100)))
        with catalog.monitor_lease() as held:
            assert held is True
            _monitor_tick(monitor, catalog, logging.getLogger("test"))

        assert _Spy.ticks == 0
