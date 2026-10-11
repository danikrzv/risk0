"""Fixture-mode analyses carry the issuer and underlying layer end to end."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.analysis import get_analysis_service
from app.api.catalog import get_catalog_service
from app.fixture_mode import build_fixture_analysis_service, build_fixture_catalog_service
from app.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("RISK0_FIXTURE_CACHE_PATH", str(tmp_path / "fixture.sqlite3"))

    def no_network(*args, **kwargs):
        raise AssertionError("fixture mode must not open a network connection")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    from app.fixture_mode import fixture_morpho_client

    fixture_morpho_client.cache_clear()
    catalog = build_fixture_catalog_service()
    analysis = build_fixture_analysis_service()
    app.dependency_overrides[get_catalog_service] = lambda: catalog
    app.dependency_overrides[get_analysis_service] = lambda: analysis
    yield TestClient(app)
    app.dependency_overrides.clear()
    fixture_morpho_client.cache_clear()


def analyze(client, position, scenarios):
    response = client.post("/v1/analysis", json={"position": position, "scenarios": scenarios})
    assert response.status_code == 200, response.text
    return response.json()


def test_wsteth_borrower_graph_includes_the_steth_and_lido_layer(client):
    body = analyze(
        client,
        {
            "mode": "borrower",
            "entity_id": "wsteth-weth-965",
            "amount": "11.2",
            "collateral_amount": "10",
            "debt_amount": "11.2",
            "ltv": "0.90",
            "numeraire": "WETH",
        },
        [{"scenario_id": "protocol-impairment", "severity": "0.10"}],
    )

    graph = body["graph"]
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    edges = {(edge["source"], edge["target"], edge["relationship"]) for edge in graph["edges"]}
    wsteth = "asset:1:0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
    steth = "asset:1:0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
    lido = "protocol:1:issuer-lido"

    assert nodes[lido]["metrics"]["issuer_type"] == "protocol"
    assert nodes[lido]["metrics"]["defiscan_slug"] == "lido"
    assert (wsteth, steth, "backed_by") in edges
    assert (steth, lido, "backed_by") in edges
    assert lido in body["scenario_results"][0]["affected_path"]


def test_belem_vault_reports_unregistered_collateral_as_unmodelled(client):
    body = analyze(
        client,
        {"mode": "lender", "entity_id": "belem-rwa-eurcv", "amount": "100000", "numeraire": "EURCV"},
        [{"scenario_id": "stablecoin-depeg", "severity": "0.10"}],
    )

    warnings = body["graph"]["completeness_warnings"]
    nodes = {node["node_id"] for node in body["graph"]["nodes"]}
    assert "protocol:1:issuer-sg-forge" in nodes
    assert any("mGLOeuro" in warning and "unmodelled" in warning for warning in warnings)
