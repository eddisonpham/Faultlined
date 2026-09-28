import pytest

from data_engine.jobs.state import ALLOWED_TRANSITIONS, TERMINAL_STATES, JobState, JobType


@pytest.mark.unit
def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.TIMED_OUT,
    } == TERMINAL_STATES
    assert all(not ALLOWED_TRANSITIONS[state] for state in TERMINAL_STATES)


@pytest.mark.unit
def test_job_state_machine_contains_expected_retry_and_cancel_paths() -> None:
    assert JobState.RETRYING in ALLOWED_TRANSITIONS[JobState.RUNNING]
    assert JobState.QUEUED in ALLOWED_TRANSITIONS[JobState.RETRYING]
    assert JobState.CANCEL_REQUESTED in ALLOWED_TRANSITIONS[JobState.RUNNING]
    assert JobState.CANCELED in ALLOWED_TRANSITIONS[JobState.CANCEL_REQUESTED]
    assert JobState.SUCCEEDED in ALLOWED_TRANSITIONS[JobState.CANCEL_REQUESTED]


@pytest.mark.unit
def test_job_types_are_stable_strings() -> None:
    assert JobType.INGEST == "ingest"
    assert JobType.WORKLOAD == "workload"
