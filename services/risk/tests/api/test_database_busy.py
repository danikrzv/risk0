import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import MORPHO_BASE_URL


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def failing_service(message: str):
    class Service:
        def list_hydrated(self):
            raise sqlite3.OperationalError(message)

    return lambda: Service()


@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_locked_database_answers_503_without_a_traceback(client, message):
    from app.api.catalog import get_catalog_service

    app.dependency_overrides[get_catalog_service] = failing_service(message)

    response = client.get("/v1/catalog")

    assert response.status_code == 503
    assert response.json() == {"detail": "Risk database busy"}


def test_other_sqlite_errors_stay_server_errors(client):
    from app.api.catalog import get_catalog_service

    app.dependency_overrides[get_catalog_service] = failing_service(
        "no such table: cache_entries"
    )

    response = client.get("/v1/catalog")

    assert response.status_code == 500
    assert "cache_entries" not in response.text


def test_real_lock_contention_on_the_cache_answers_503(
    client, morpho_api, clock, tmp_path
):
    from app.adapters.morpho import MorphoClient
    from app.api.catalog import get_catalog_service
    from app.catalog.repository import CatalogRepository
    from app.catalog.service import CatalogService
    from app.infrastructure.cache import SQLiteCache
    from tests.catalog.test_service import CATALOG_PATH

    path = tmp_path / "cache.sqlite3"
    morpho = MorphoClient(
        MORPHO_BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(morpho_api.handler)),
        cache=SQLiteCache(path, timeout_seconds=0.05),
        clock=clock,
        sleep=lambda _: None,
    )
    service = CatalogService(CatalogRepository(CATALOG_PATH), morpho, clock=clock)
    app.dependency_overrides[get_catalog_service] = lambda: service
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another writer that never lets go
    try:
        response = client.get("/v1/catalog")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "Risk database busy"}
