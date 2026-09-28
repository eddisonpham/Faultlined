import json
import logging

import pytest

from data_engine.observability.logging import JsonFormatter, correlation_id_var


@pytest.mark.unit
def test_json_formatter_emits_required_fields_and_correlation_id() -> None:
    token = correlation_id_var.set("corr-123")
    try:
        record = logging.LogRecord("data.engine", logging.INFO, "", 0, "hello", (), None)
        formatted = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id_var.reset(token)

    assert formatted["level"] == "info"
    assert formatted["service"] == "data-engine"
    assert formatted["event"] == "data_engine"
    assert formatted["message"] == "hello"
    assert formatted["correlation_id"] == "corr-123"
    assert formatted["timestamp"].endswith("+00:00")


@pytest.mark.unit
def test_json_formatter_includes_job_episode_and_error_fields() -> None:
    try:
        raise ValueError("bad episode")
    except ValueError:
        import sys

        record = logging.LogRecord("worker", logging.ERROR, "", 0, "failed", (), sys.exc_info())
    record.job_id = "job-1"
    record.episode_id = "episode-1"
    record.reason_code = "INGEST_PARSE_FAILED"
    formatted = json.loads(JsonFormatter().format(record))
    assert formatted["job_id"] == "job-1"
    assert formatted["episode_id"] == "episode-1"
    assert formatted["reason_code"] == "INGEST_PARSE_FAILED"
    assert formatted["error"]["type"] == "ValueError"
