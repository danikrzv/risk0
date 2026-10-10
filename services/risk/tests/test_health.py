import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_contract(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "service": "risk0-risk", "version": "0.1.0"
    }
