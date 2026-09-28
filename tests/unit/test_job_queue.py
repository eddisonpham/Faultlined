from unittest.mock import MagicMock

import pytest

from data_engine.jobs.queue import PostgresJobQueue


@pytest.mark.unit
def test_queue_submit_and_claim_delegate_to_catalog() -> None:
    catalog = MagicMock()
    catalog.submit_job.return_value = ({"id": "job-1"}, True)
    catalog.claim_job.return_value = {"id": "job-2"}
    queue = PostgresJobQueue()
    queue.catalog = catalog

    submitted = queue.submit("ingest", {"episode": {}}, "key", "corr")
    claimed = queue.claim()

    assert submitted == ({"id": "job-1"}, True)
    assert claimed == {"id": "job-2"}
    catalog.submit_job.assert_called_once_with("ingest", {"episode": {}}, "key", "corr")
    catalog.claim_job.assert_called_once_with()
