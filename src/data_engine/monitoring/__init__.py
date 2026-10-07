"""Deterministic notifier for unattended curation (ADR 0020)."""

from data_engine.monitoring.baselines import Baseline, BaselineBook
from data_engine.monitoring.contracts import Expectation, InvalidExpectation, RunOutcome
from data_engine.monitoring.contracts import evaluate as evaluate_contract
from data_engine.monitoring.detectors import (
    DEFAULT_CONFIG as DEFAULT_DETECTOR_CONFIG,
)
from data_engine.monitoring.detectors import DETECTORS, DetectorConfig, DetectorContext, detect
from data_engine.monitoring.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    FeatureInputs,
    FeatureVector,
    ResourceSample,
    build_features,
)
from data_engine.monitoring.queue import (
    ExistingIncident,
    TriageAction,
    TriageOutcome,
    TriagePolicy,
    fingerprint,
)
from data_engine.monitoring.service import MonitorConfig, MonitorService, TickReport
from data_engine.monitoring.signals import (
    NOTIFY_LABELS,
    Label,
    NotifyClass,
    Severity,
    Signal,
)

__all__ = [
    "DEFAULT_DETECTOR_CONFIG",
    "DETECTORS",
    "FEATURE_NAMES",
    "FEATURE_SCHEMA_VERSION",
    "NOTIFY_LABELS",
    "Baseline",
    "BaselineBook",
    "DetectorConfig",
    "DetectorContext",
    "ExistingIncident",
    "Expectation",
    "FeatureInputs",
    "FeatureVector",
    "InvalidExpectation",
    "Label",
    "MonitorConfig",
    "MonitorService",
    "NotifyClass",
    "ResourceSample",
    "RunOutcome",
    "Severity",
    "Signal",
    "TickReport",
    "TriageAction",
    "TriageOutcome",
    "TriagePolicy",
    "build_features",
    "detect",
    "evaluate_contract",
    "fingerprint",
]
