import pytest
from fastapi.testclient import TestClient

from data_engine.api.app import create_app
from data_engine.config import Settings


class CatalogStub:
    pass


@pytest.mark.unit
def test_create_app_has_expected_title_and_version() -> None:
    app = create_app(Settings(_env_file=None), initialize_database=False, catalog=CatalogStub())  # type: ignore[arg-type]
    assert app.title == "Faultlined"
    assert app.version == "0.1.0"
    assert isinstance(app.state.catalog, CatalogStub)


@pytest.mark.unit
def test_health_endpoint_avoids_db_when_disabled() -> None:
    client = TestClient(create_app(Settings(_env_file=None), initialize_database=False))
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
