"""The worker's success path must not assume every job produces an episode.

`process_one` used to read `result["episode_id"]` unconditionally. That happened
to work while every job result happened to be shaped like an ingest result, and
it broke the moment a validate job started reporting over a *selection*: the job
did its whole job correctly, was then recorded as failed with a KeyError, and the
operator saw a failure for work that succeeded.
"""

from __future__ import annotations

import re

import pytest

from data_engine.jobs.worker import IngestWorker

pytestmark = pytest.mark.unit


def _code(func: object) -> str:
    """The body of a function with comments and docstrings removed.

    Assertions here have to read code, not prose: a fix whose explanation names
    the bug it fixed will otherwise fail its own regression test.
    """
    import inspect

    source = inspect.getsource(func)  # type: ignore[arg-type]
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    return re.sub(r"^\s*#.*$", "", source, flags=re.M)


def test_the_success_path_tolerates_a_result_with_no_episode() -> None:
    """A build or validate result has no top-level episode id, and that is fine."""
    code = _code(IngestWorker.process_one)
    assert 'result["episode_id"]' not in code, (
        "indexing episode_id unconditionally fails any job that is not an ingest"
    )
    assert 'result.get("episode_id")' in code


def test_a_validate_result_reports_its_selection_not_one_episode() -> None:
    """The aggregate shape is the contract; it must survive the round trip."""
    code = _code(IngestWorker._validate)
    assert '"episodes": outcomes' in code
    assert '"checked": len(outcomes)' in code
