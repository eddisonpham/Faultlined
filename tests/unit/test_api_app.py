import pytest

from data_engine.api.app import create_app


@pytest.mark.unit
def test_create_app_has_expected_title_and_version() -> None:
    app = create_app()
    assert app.title == "Robot Episode Data Engine"
    assert app.version == "0.1.0"
