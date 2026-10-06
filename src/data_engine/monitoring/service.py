"""The evaluation tick: one pass of the notifier over the current window.

Everything the notifier does happens here, in a fixed order, and the order is the
design:

1. **Probe the catalog.** A failure produces ``DATABASE_UNREACHABLE`` and nothing
   else — every catalog-derived feature is untrustworthy that tick, and reporting
   "all quiet" from a monitor that cannot see is the one outcome that must never
   happen.
2. **Build the feature vector** from the window's metric records and the snapshot.
3. **Evaluate every rule**, then every pending completion contract.
4. **Triage**, which is where precision is actually enforced.
5. **Persist** incidents and control limits, then emit the monitor's own metrics.

Two properties are worth stating because they are what make this safe to leave
running unattended:

* **The monitor is out of band.** Nothing in a tick can fail a build. A tick that
  raises is a lost observation; a tick that *propagates* would be a lost build.
  Everything is caught, counted, and reported through the health endpoint.
* **The book survives a database outage.** Baselines are reloaded from the catalog
  when it is reachable and kept in memory when it is not, so a brief outage blips
  the incident store rather than resetting every control limit to cold.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from data_engine.monitoring import contracts as contract_checks
from data_engine.monitoring.baselines import BaselineBook
from data_engine.monitoring.contracts import Expectation, RunOutcome
from data_engine.monitoring.detectors import (
    DEFAULT_CONFIG as DEFAULT_DETECTOR_CONFIG,
)
from data_engine.monitoring.detectors import (
    DETECTORS,
    DetectorConfig,
    DetectorContext,
    detect,
    observations_for,
)
from data_engine.monitoring.features import (
    DEFAULT_WINDOW_SECONDS,
    FEATURE_SCHEMA_VERSION,
    FeatureInputs,
    FeatureVector,
    ResourceSample,
    build_features,
)
from data_engine.monitoring.queue import (
    ExistingIncident,
    TriageOutcome,
    TriagePolicy,
    as_mapping,
    budget_exhausted,
    decide,
    reasons,
)
from data_engine.monitoring.signals import NOTIFY_LABELS, Label, Signal
from data_engine.monitoring.summary import render_notify, render_summary
from data_engine.observability.aggregate import (
    MONITOR_BLIND_METRIC,
    MONITOR_TICK_METRIC,
    newest_metric_at,
    newest_record,
    read_metric_records,
    window_records,
)
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.observability.telemetry import sample_resources

logger = logging.getLogger(__name__)

#: A monitor with no telemetry at all is blind; below this the window is treated
#: as blind rather than as "nothing happened".
SINK_LAG_BLIND_SECONDS = 600.0

#: How far back the health read-back looks for the newest tick record. A tick every
#: 60 s with many job metrics between them means the newest tick can sit a few
#: thousand records down the sink; this is a bounded tail read, not a history scan.
HEALTH_TAIL_RECORDS = 20_000


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    tick_seconds: float = DEFAULT_WINDOW_SECONDS
    budget_window_seconds: float = 3600.0
    max_records: int = 20_000
    policy: TriagePolicy = field(default_factory=TriagePolicy)
    detector: DetectorConfig = field(default_factory=lambda: DEFAULT_DETECTOR_CONFIG)


DEFAULT_CONFIG = MonitorConfig()


@dataclass(frozen=True, slots=True)
class TickReport:
    """Everything one tick decided, for the API, the UI, and the tests."""

    observed_at: datetime
    features: FeatureVector
    signals: list[Signal] = field(default_factory=list)
    outcomes: list[TriageOutcome] = field(default_factory=list)
    written: list[dict[str, Any]] = field(default_factory=list)
    contract_outcomes: list[dict[str, Any]] = field(default_factory=list)
    catalog_reachable: bool = True
    blind: bool = False
    suppressed: dict[str, int] = field(default_factory=dict)
    budget_exhausted: bool = False
    warm_scopes: int = 0
    held_scopes: int = 0
    notify_ready: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "catalog_reachable": self.catalog_reachable,
            "blind": self.blind,
            "sensor_availability": self.features.value("sensor_availability", 1.0),
            "sink_lag_seconds": self.features.value("sink_lag_seconds", -1.0),
            "warm_scopes": self.warm_scopes,
            "held_scopes": self.held_scopes,
            "signals": len(self.signals),
            "written": len(self.written),
            "suppressed": dict(self.suppressed),
            "budget_exhausted": self.budget_exhausted,
            "contract_outcomes": list(self.contract_outcomes),
            "notify_ready": [row["id"] for row in self.notify_ready],
            "duration_seconds": self.duration_seconds,
        }


class MonitorService:
    """Runs ticks against a catalog; safe to call from a loop or from a test."""

    def __init__(
        self,
        catalog: Any,
        *,
        metrics: RuntimeMetrics | None = None,
        metrics_path: Path | None = None,
        config: MonitorConfig = DEFAULT_CONFIG,
        now: datetime | None = None,
    ) -> None:
        self._catalog = catalog
        self._metrics = metrics or RuntimeMetrics()
        self._metrics_path = metrics_path
        self._config = config
        self._now = now
        self._book: BaselineBook | None = None
        self._last_tick: datetime | None = None
        self._last_blind: bool | None = None

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    # ---- one pass -----------------------------------------------------------

    def _cat(self, override: Any | None = None) -> Any:
        """The catalog to use for this tick.

        An override exists because ``app.state.catalog`` is swappable — the API
        tests wire a stub in after the app is built — and a monitor holding a
        stale reference would quietly read the wrong database.
        """
        return override if override is not None else self._catalog

    def tick(self, catalog: Any | None = None) -> TickReport:
        started = time.perf_counter()
        reference = self.now()
        cat = self._cat(catalog)

        snapshot, reachable = self._probe(reference, cat)
        records = self._records(reference)
        features = build_features(
            FeatureInputs(
                now=reference,
                records=records,
                snapshot=snapshot,
                resources=self._resources(),
                window_seconds=self._config.tick_seconds,
                catalog_reachable=reachable,
            )
        )

        book = self._load_book(reachable, cat)
        book.release_expired(now=reference.timestamp())

        signals = detect(
            DetectorContext(
                now=reference,
                features=features,
                baselines=book,
                running=list(snapshot.get("running") or []),
                config=self._config.detector,
            )
        )
        contract_signals, contract_rows = self._contracts(reference, reachable, cat)
        signals.extend(contract_signals)

        outcomes = self._triage(signals, reference, cat)
        written = self._persist(outcomes, reference, cat)
        self._update_baselines(book, outcomes, reference, features)
        if reachable:
            self._save_book(book, cat)

        blind = self._is_blind(features)
        notify_ready = [row for row in written if row["notify_class"] == "notify"]
        report = TickReport(
            observed_at=reference,
            features=features,
            signals=signals,
            outcomes=outcomes,
            written=written,
            contract_outcomes=contract_rows,
            catalog_reachable=reachable,
            blind=blind,
            suppressed=reasons(outcomes),
            budget_exhausted=budget_exhausted(outcomes),
            warm_scopes=len(book.warm_scopes()),
            held_scopes=sum(1 for item in book.values() if item.breached),
            notify_ready=notify_ready,
            duration_seconds=time.perf_counter() - started,
        )
        self._emit(report)
        self._last_tick = reference
        self._last_blind = report.blind
        return report

    def notify_preview(self, catalog: Any | None = None) -> str:
        """What a notifier would send right now. Rendering only; nothing is sent.

        The delivery path is deliberately not implemented (ADR 0020 §10.3): email
        requires explicit owner authorization, and this exists so the dry run is a
        real code path rather than a description of one.
        """
        rows = self._cat(catalog).list_incidents(status="open", limit=50)
        return render_notify([row for row in rows if row["notify_class"] == "notify"])

    def health(self) -> dict[str, Any]:
        """The monitor's own state; a monitor that silently stops is the worst outcome.

        The tick usually runs in a **different process** from the one serving this
        endpoint - the worker loop ticks, the API answers (ADR 0031) - so an
        in-memory answer would read `last_tick_at: null` and `blind: no` about a
        monitor that is running perfectly. Both facts are therefore read back from
        the metric sink, which is the one record every process shares (ADR 0017),
        and the in-process values are used only when this process has ticked itself.
        """
        book = self._book
        # One tail read serves both facts. They used to be read separately, which parsed
        # the same window twice on every call - and this is called on every render of
        # `/ui/incidents`, a page that polls, as well as by the endpoint: measured at
        # 42 ms p50 on a 5k-record sink and 97 ms at the 20k bound, half of it duplicated.
        # A process that has ticked itself needs no read at all.
        records = (
            self._health_records() if self._last_tick is None or self._last_blind is None else []
        )
        last = self._last_tick or newest_metric_at(records, name=MONITOR_TICK_METRIC)
        blind: bool | None
        if self._last_blind is not None:
            blind = self._last_blind
        else:
            record = newest_record(records, name=MONITOR_BLIND_METRIC)
            blind = bool(record.get("value")) if record else None
        return {
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "last_tick_at": last.isoformat() if last else None,
            "blind": blind,
            "tick_seconds": self._config.tick_seconds,
            "baseline_scopes": len(book) if book else 0,
            "warm_scopes": len(book.warm_scopes()) if book else 0,
            "held_scopes": sum(1 for item in book.values() if item.breached) if book else 0,
            "alert_budget_per_window": self._config.policy.budget_per_window,
            "budget_window_seconds": self._config.budget_window_seconds,
            "notify_labels": sorted(NOTIFY_LABELS),
            "detectors": [name for name, _ in DETECTORS],
        }

    def _health_records(self) -> list[dict[str, Any]]:
        """The bounded tail of the shared sink, read once per `health()` call.

        Bounded because health is served on a page load: a search that walked a whole
        history would make the health check the slowest thing on the page, and a monitor
        that costs more than it watches is its own defect.
        """
        if self._metrics_path is None:
            return []
        try:
            return read_metric_records(self._metrics_path, max_records=HEALTH_TAIL_RECORDS)
        except OSError:
            return []

    # ---- steps --------------------------------------------------------------

    def _probe(self, reference: datetime, catalog: Any) -> tuple[dict[str, Any], bool]:
        try:
            return dict(catalog.monitoring_snapshot(now=reference)), True
        except Exception as exc:
            logger.warning(
                "monitoring probe failed",
                extra={"event": "monitor_probe_failed", "error_type": type(exc).__name__},
            )
            return {}, False

    def _records(self, reference: datetime) -> list[dict[str, Any]]:
        if self._metrics_path is None:
            return []
        try:
            records = read_metric_records(self._metrics_path, max_records=self._config.max_records)
        except OSError as exc:
            logger.warning(
                "metric sink unreadable",
                extra={"event": "monitor_sink_unreadable", "error_type": type(exc).__name__},
            )
            return []
        since = reference - timedelta(seconds=self._config.tick_seconds)
        return window_records(records, since=since)

    def _resources(self) -> ResourceSample | None:
        try:
            sample = sample_resources()
        except Exception as exc:
            logger.warning(
                "resource sampling failed",
                extra={"event": "monitor_resources_failed", "error_type": type(exc).__name__},
            )
            return None
        return ResourceSample(
            cpu_percent=sample.cpu_percent,
            memory_used_bytes=sample.memory_used_bytes,
            memory_available_bytes=sample.memory_available_bytes,
            disk_free_bytes=sample.disk_free_bytes,
        )

    def _load_book(self, reachable: bool, catalog: Any) -> BaselineBook:
        if reachable:
            try:
                self._book = BaselineBook.from_rows(catalog.load_baselines())
            except Exception as exc:
                logger.warning(
                    "baseline load failed",
                    extra={
                        "event": "monitor_baseline_load_failed",
                        "error_type": type(exc).__name__,
                    },
                )
        if self._book is None:
            self._book = BaselineBook()
        return self._book

    def _save_book(self, book: BaselineBook, catalog: Any) -> None:
        try:
            catalog.save_baselines([item.to_row() for item in book.values()])
        except Exception as exc:
            logger.warning(
                "baseline save failed",
                extra={"event": "monitor_baseline_save_failed", "error_type": type(exc).__name__},
            )

    def _contracts(
        self, reference: datetime, reachable: bool, catalog: Any
    ) -> tuple[list[Signal], list[dict[str, Any]]]:
        if not reachable:
            return [], []
        try:
            rows = catalog.pending_contract_outcomes()
        except Exception as exc:
            logger.warning(
                "contract read failed",
                extra={"event": "monitor_contract_read_failed", "error_type": type(exc).__name__},
            )
            return [], []

        signals: list[Signal] = []
        records: list[dict[str, Any]] = []
        for row in rows:
            expectation = Expectation.from_dict(dict(row))
            state = str(row.get("job_state") or "")
            settled = state in {"succeeded", "failed", "canceled", "timed_out"}
            outcome = RunOutcome(
                job_id=str(row["job_id"]),
                state=state,
                settled=settled,
                episodes_produced=int(row.get("episodes_produced") or 0),
                episodes_valid=int(row.get("episodes_valid") or 0),
                duration_seconds=_duration(row, reference, settled),
                now=reference,
            )
            result, found = contract_checks.evaluate(expectation, outcome)
            observed = {
                "episodes_produced": outcome.episodes_produced,
                "episodes_valid": outcome.episodes_valid,
                "duration_seconds": outcome.duration_seconds,
                "job_state": state,
            }
            signals.extend(found)
            if settled and result != contract_checks.OUTCOME_PENDING:
                catalog.set_contract_outcome(str(row["job_id"]), result, observed)
            records.append(
                {
                    "job_id": str(row["job_id"]),
                    "outcome": result,
                    "observed": observed,
                    "signals": [signal.label for signal in found],
                }
            )
        return signals, records

    def _triage(
        self, signals: Sequence[Signal], reference: datetime, catalog: Any
    ) -> list[TriageOutcome]:
        existing: dict[str, ExistingIncident] = {}
        budget_used = 0
        try:
            existing = as_mapping(catalog.unresolved_incidents())
            budget_used = catalog.incidents_opened_since(
                reference - timedelta(seconds=self._config.budget_window_seconds)
            )
        except Exception as exc:
            logger.warning(
                "triage state unavailable",
                extra={"event": "monitor_triage_state_failed", "error_type": type(exc).__name__},
            )
        return decide(
            signals,
            now=reference,
            policy=self._config.policy,
            open_incidents=existing,
            budget_used=budget_used,
        )

    def _persist(
        self, outcomes: Sequence[TriageOutcome], reference: datetime, catalog: Any
    ) -> list[dict[str, Any]]:
        written: list[dict[str, Any]] = []
        for outcome in outcomes:
            if outcome.action.value not in {"open", "bump"}:
                continue
            signal = outcome.signal
            summary = render_summary(signal.label, signal.evidence, signal.detail)
            try:
                row = catalog.upsert_incident(
                    incident_id=str(outcome.incident.id) if outcome.incident else str(uuid4()),
                    fingerprint=outcome.fingerprint,
                    label=signal.label,
                    severity=signal.severity.value,
                    notify_class=signal.notify_class.value,
                    scope=signal.scope,
                    summary=summary,
                    evidence=signal.to_evidence(),
                    feature_schema_version=signal.feature_schema_version,
                    seen_at=reference,
                )
            except Exception as exc:
                logger.warning(
                    "incident persist failed",
                    extra={
                        "event": "monitor_persist_failed",
                        "label": signal.label,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            written.append(row)
        return written

    def _update_baselines(
        self,
        book: BaselineBook,
        outcomes: Sequence[TriageOutcome],
        reference: datetime,
        features: FeatureVector,
    ) -> None:
        """Hold the limits a signal breached, release the rest, then observe.

        Holding is what stops a sustained fault redefining what normal looks like.
        Releasing is unconditional, so a resolved fault cannot be pinned open by a
        stale hold; ``release_expired`` is the backstop if a tick never runs at all.

        Observation happens *after* the hold/release decision, and ``observe``
        itself refuses to absorb a held scope — so a breach freezes its own limit
        for exactly one tick too many, which is deliberate: the freeze must outlast
        the signal that caused it or it is not a freeze.
        """
        held: set[tuple[str, str]] = set()
        for outcome in outcomes:
            if outcome.action.value in {"open", "bump"}:
                held |= _held_keys(outcome.signal)
        for feature, scope in book:
            if (feature, scope) in held:
                book.hold(feature, scope=scope, now=reference.timestamp())
            else:
                book.release(feature, scope=scope)
        book.observe_many(observations_for(features))

    def _is_blind(self, features: FeatureVector) -> bool:
        if not features.catalog_reachable:
            return True
        if not features.has("sink_lag_seconds"):
            return True
        return features.value("sink_lag_seconds") > SINK_LAG_BLIND_SECONDS

    def _emit(self, report: TickReport) -> None:
        self._metrics.monitor_tick(report.duration_seconds)
        self._metrics.monitor_sensor_availability(report.features.value("sensor_availability", 1.0))
        self._metrics.monitor_blind(report.blind)
        for signal in report.signals:
            self._metrics.monitor_signal(signal.label, signal.severity)
        for row in report.written:
            self._metrics.incident_opened(row["label"], str(row["severity"]), row["notify_class"])
        for action, count in report.suppressed.items():
            self._metrics.monitor_suppressed(action, count)


def _held_keys(signal: Signal) -> set[tuple[str, str]]:
    """Which control limits this signal's breach should freeze."""
    if signal.label == Label.METRIC_SHIFT.value:
        return {(str(item.get("feature")), "") for item in (signal.evidence.get("shifted") or [])}
    if signal.label == Label.QUALITY_SHIFT.value:
        return {(str(signal.evidence.get("feature", "")), "")}
    if signal.label == Label.QUARANTINE_RATE_HIGH.value:
        return {("quarantine_rate", "")}
    if signal.label == Label.FRAME_COUNT_COLLAPSE.value:
        return {("frame_count_median", "")}
    if signal.label == Label.RESOURCE_DEGRADED.value:
        return {("cpu_percent", ""), ("memory_used_ratio", "")}
    return set()


def _duration(row: dict[str, Any], reference: datetime, settled: bool) -> float | None:
    started = row.get("started_at")
    if not isinstance(started, datetime):
        return None
    end = row.get("finished_at") if settled else reference
    if not isinstance(end, datetime):
        return None
    return (end - started).total_seconds()
