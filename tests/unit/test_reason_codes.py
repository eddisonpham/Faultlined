import pytest

from data_engine.observability.reason_codes import ReasonCode


@pytest.mark.unit
def test_reason_codes_serialize_as_stable_strings() -> None:
    assert ReasonCode.INGEST_PARSE_FAILED == "INGEST_PARSE_FAILED"
    assert ReasonCode.VALIDATION_PROFILE_INVALID == "VALIDATION_PROFILE_INVALID"
    assert ReasonCode.GPU_UNAVAILABLE == "GPU_UNAVAILABLE"
    assert ReasonCode.IDEMPOTENCY_KEY_CONFLICT == "IDEMPOTENCY_KEY_CONFLICT"
