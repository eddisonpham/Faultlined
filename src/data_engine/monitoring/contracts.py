"""Completion contracts: the highest-value check in the notifier, and the cheapest.

During unattended curation the operative question is not "did a metric move" but
**did the thing I asked for happen**. A contract makes that question exact: the
owner declares an expectation, the run settles, and the two are compared.

That comparison is arithmetic, so it is deterministic, interpretable, and free of
threshold tuning — which is why it anchors the taxonomy rather than sitting
alongside it. An anomaly-shaped signal with no contract behind it is, by
construction, the ambiguous case, and the triage gate treats it accordingly.

Each breach maps to exactly one label so a single fault cannot open two incidents:

* fewer valid episodes than declared -> ``CONTRACT_BREACH`` (critical, notify)
* valid fraction below the declared floor -> ``PARTIAL_SUCCESS`` (high, queue)
* deadline passed or duration exceeded -> ``TIME_MISSED`` (medium, queue)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from data_engine.monitoring.signals import Label, NotifyClass, Severity, Signal

MAX_EXPECTED_EPISODES = 1_000_000


class InvalidExpectation(ValueError):
    """A declared expectation is outside the range the evaluator can honour."""


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the owner expects a run to produce. Every field is optional."""

    expected_episodes: int | None = None
    expected_valid_fraction: float | None = None
    max_duration_seconds: float | None = None
    deadline_at: datetime | None = None

    def validate(self) -> None:
        if self.expected_episodes is not None and not (
            0 <= self.expected_episodes <= MAX_EXPECTED_EPISODES
        ):
            raise InvalidExpectation(
                f"expected_episodes must be between 0 and {MAX_EXPECTED_EPISODES}"
            )
        if self.expected_valid_fraction is not None and not (
            0.0 <= self.expected_valid_fraction <= 1.0
        ):
            raise InvalidExpectation("expected_valid_fraction must be between 0.0 and 1.0")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise InvalidExpectation("max_duration_seconds must be positive")
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise InvalidExpectation("deadline_at must be timezone-aware")

    def declared_fields(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "expected_episodes",
                "expected_valid_fraction",
                "max_duration_seconds",
                "deadline_at",
            )
            if getattr(self, name) is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_episodes": self.expected_episodes,
            "expected_valid_fraction": self.expected_valid_fraction,
            "max_duration_seconds": self.max_duration_seconds,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Expectation:
        raw_deadline = data.get("deadline_at")
        deadline = None
        if isinstance(raw_deadline, str) and raw_deadline:
            deadline = datetime.fromisoformat(raw_deadline)
        return cls(
            expected_episodes=_as_int(data.get("expected_episodes")),
            expected_valid_fraction=_as_float(data.get("expected_valid_fraction")),
            max_duration_seconds=_as_float(data.get("max_duration_seconds")),
            deadline_at=deadline,
        )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What actually happened to a contracted run at tick time."""

    job_id: str
    state: str
    settled: bool
    episodes_produced: int = 0
    episodes_valid: int = 0
    duration_seconds: float | None = None
    now: datetime | None = None


#: Outcomes a contract can reach. ``pending`` is the only non-terminal value.
OUTCOME_PENDING = "pending"
OUTCOME_MET = "met"
OUTCOME_SHORT = "short"
OUTCOME_DEGRADED = "degraded"
OUTCOME_MISSED = "missed"


def evaluate(expectation: Expectation, outcome: RunOutcome) -> tuple[str, list[Signal]]:
    """Compare a declared expectation with an observed outcome.

    Returns ``(outcome, signals)``. The first breach wins, so one fault produces
    one incident rather than several overlapping ones: a run that is both short
    and late is reported once, with both facts in its evidence.

    **Episode-count expectations are only checked once the run has settled.** "I
    expect 200 episodes" is a claim about a *finished* run; a job that is still
    queued has produced nothing *yet*, and calling that a breach would fire an
    incident the instant a contract is declared. A run that never settles is a
    different fault with a different detector (``RUN_STALLED``, via its deadline),
    so the contract stays quiet about it until the deadline decides the question.
    """
    if not expectation.declared_fields():
        return OUTCOME_PENDING, []
    expectation.validate()

    produced = max(0, outcome.episodes_produced)
    valid = max(0, min(outcome.episodes_valid, produced))

    if (
        expectation.expected_episodes is not None
        and outcome.settled
        and valid < expectation.expected_episodes
    ):
        return OUTCOME_SHORT, [
            Signal(
                label=Label.CONTRACT_BREACH.value,
                severity=Severity.CRITICAL,
                scope=outcome.job_id,
                detail=(
                    f"run produced {valid} valid episodes, "
                    f"{expectation.expected_episodes} were expected"
                ),
                evidence={
                    "expected_episodes": expectation.expected_episodes,
                    "valid_episodes": valid,
                    "produced_episodes": produced,
                    "shortfall": expectation.expected_episodes - valid,
                    "job_state": outcome.state,
                    "duration_seconds": outcome.duration_seconds,
                },
                notify=NotifyClass.NOTIFY,
            )
        ]

    if (
        expectation.expected_valid_fraction is not None
        and outcome.settled
        and produced > 0
        and (valid / produced) < expectation.expected_valid_fraction
    ):
        observed_fraction = valid / produced
        return OUTCOME_DEGRADED, [
            Signal(
                label=Label.PARTIAL_SUCCESS.value,
                severity=Severity.HIGH,
                scope=outcome.job_id,
                detail=(
                    f"run succeeded but only {observed_fraction:.0%} of episodes are valid, "
                    f"below the declared {expectation.expected_valid_fraction:.0%}"
                ),
                evidence={
                    "expected_valid_fraction": expectation.expected_valid_fraction,
                    "observed_valid_fraction": observed_fraction,
                    "valid_episodes": valid,
                    "produced_episodes": produced,
                    "job_state": outcome.state,
                },
            )
        ]

    if not outcome.settled:
        # A deadline is a claim about work that has *not* finished, so it is the
        # only timing check that applies to a live run.
        now = outcome.now
        if (
            expectation.deadline_at is not None
            and now is not None
            and now > (expectation.deadline_at)
        ):
            return OUTCOME_MISSED, [
                Signal(
                    label=Label.TIME_MISSED.value,
                    severity=Severity.MEDIUM,
                    scope=outcome.job_id,
                    detail=f"deadline passed while the run was still {outcome.state}",
                    evidence={
                        "deadline_at": expectation.deadline_at.isoformat(),
                        "observed_at": now.isoformat(),
                        "job_state": outcome.state,
                        "overdue_seconds": (now - expectation.deadline_at).total_seconds(),
                    },
                )
            ]
        return OUTCOME_PENDING, []

    # A duration budget is a claim about the *finished* run, so it is checked
    # whether or not the run is still live: a run that took ten times its budget
    # has missed it, and reporting "met" would be reporting the absence of a
    # check rather than the presence of a pass.
    if (
        expectation.max_duration_seconds is not None
        and outcome.duration_seconds is not None
        and outcome.duration_seconds > expectation.max_duration_seconds
    ):
        return OUTCOME_MISSED, [
            Signal(
                label=Label.TIME_MISSED.value,
                severity=Severity.MEDIUM,
                scope=outcome.job_id,
                detail=(
                    f"run took {outcome.duration_seconds:.0f}s, over the declared "
                    f"{expectation.max_duration_seconds:.0f}s"
                ),
                evidence={
                    "max_duration_seconds": expectation.max_duration_seconds,
                    "duration_seconds": outcome.duration_seconds,
                    "job_state": outcome.state,
                },
            )
        ]

    return OUTCOME_MET, []


def _as_int(value: object) -> int | None:
    return None if value is None else int(cast(Any, value))


def _as_float(value: object) -> float | None:
    return None if value is None else float(cast(Any, value))
