import pytest

from data_engine.catalog.repository import canonical_json


@pytest.mark.unit
def test_canonical_json_sorts_keys_and_uses_stable_separators() -> None:
    assert canonical_json({"z": 2, "a": 1}) == b'{"a":1,"z":2}'


@pytest.mark.unit
def test_canonical_json_rejects_non_finite_floats() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json({"value": float("nan")})
