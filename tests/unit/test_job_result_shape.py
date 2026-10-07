"""The worker's success path must not assume every job produces an episode."""

from __future__ import annotations

import re

import pytest

from data_engine.jobs.worker import IngestWorker

pytestmark = pytest.mark.unit


def _code(func: object) -> str:
    """The body of a function with comments and docstrings removed."""
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
