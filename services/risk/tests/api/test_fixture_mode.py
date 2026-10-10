"""Fixture mode (`RISK0_DATA_MODE=fixture`): the unchanged API and services over recorded data."""

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.analysis import get_analysis_service, get_default_service
from app.api.catalog import get_catalog_service
from app.fixture_mode import (
    DATA_MODE_ENV,
    DEGRADED_MARKET_ID,
    DataModeError,
    build_fixture_analysis_service,
    build_fixture_catalog_service,
    data_mode,
)
from app.main import app


DEFAULT_SCENARIOS = [
    {"scenario_id": "stablecoin-depeg", "severity": "0.10"},
    {"scenario_id": "collateral-shock", "severity": "0.15"},
    {"scenario_id": "oracle-failure", "severity": "0.05"},
    {"scenario_id": "liquidity-collapse", "severity": "0.80"},
    {"scenario_id": "protocol-impairment", "severity": "0.10"},
]


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


def analyze(client, position, scenarios=DEFAULT_SCENARIOS):
    response = client.post("/v1/analysis", json={"position": position, "scenarios": scenarios})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "live"), ("", "live"), ("live", "live"), ("fixture", "fixture"), (" Fixture ", "fixture")],
)
def test_data_mode_defaults_to_live(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(DATA_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(DATA_MODE_ENV, value)

    assert data_mode() == expected


def test_unknown_data_mode_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv(DATA_MODE_ENV, "demo")

    with pytest.raises(DataModeError):
        data_mode()


def test_unknown_data_mode_stops_the_service_at_startup(monkeypatch):
    monkeypatch.setenv(DATA_MODE_ENV, "fixtrue")

    with pytest.raises(DataModeError):
        with TestClient(app):
            pass


def test_unknown_data_mode_gives_a_sanitized_catalog_503(monkeypatch):
    monkeypatch.setenv(DATA_MODE_ENV, "fixtrue")
    get_catalog_service.cache_clear()
    try:
        response = TestClient(app).get("/v1/catalog")
    finally:
        get_catalog_service.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The curated catalog is temporarily unavailable."}
    assert "RISK0_DATA_MODE" not in response.text


def test_fixture_mode_selects_the_fixture_wiring(monkeypatch, tmp_path):
    from app.fixture_mode import FixtureOracles, fixture_morpho_client

    monkeypatch.setenv(DATA_MODE_ENV, "fixture")
    monkeypatch.setenv("RISK0_FIXTURE_CACHE_PATH", str(tmp_path / "fixture.sqlite3"))
    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)
    fixture_morpho_client.cache_clear()
    get_default_service.cache_clear()
    try:
        service = get_default_service()
        assert isinstance(service._oracles, FixtureOracles)
    finally:
        get_default_service.cache_clear()
        fixture_morpho_client.cache_clear()


def test_fixture_catalog_hydrates_every_mainnet_entry_from_recorded_data(client):
    response = client.get("/v1/catalog")

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 17
    for entry in entries:
        if entry["chain_id"] == 1:
            assert entry["availability"] == "available", entry["entity_id"]
            assert entry["source"].startswith("https://morpho.fixture.invalid/")
        else:
            assert entry["analysis_available"] is False


def test_fixture_belem_lender_with_default_scenarios_stays(client):
    body = analyze(
        client,
        {"mode": "lender", "entity_id": "belem-rwa-eurcv", "amount": "100000", "numeraire": "EURCV"},
    )

    recommendation = body["recommendation"]
    assert recommendation["decision"] == "STAY"
    assert recommendation["triggers"] == []
    assert Decimal(recommendation["expected_loss"]) < Decimal(recommendation["exit_cost"])
    # Allocated markets are recorded, so no exposure is unknown.
    assert not any(node["metrics"].get("exposure_unknown") for node in body["graph"]["nodes"])
    assert {result["probability"]["source"] for result in body["scenario_results"]} == {
        "fixture:probabilities"
    }


def test_fixture_wsteth_borrower_at_the_ui_preset_exits_on_liquidation(client):
    # apps/web/src/components/position-form.tsx POSITION_PRESETS["wsteth-weth-965"]
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
        [{"scenario_id": "collateral-shock", "severity": "0.15"}],
    )

    assert body["recommendation"]["decision"] == "EXIT"
    assert body["recommendation"]["triggers"] == ["collateral-shock:liquidation"]
    assert body["exit_estimate"]["freshness"] == "fresh"


def test_fixture_degraded_market_is_unavailable_after_the_failed_refresh(client):
    body = analyze(
        client,
        {
            "mode": "borrower",
            "entity_id": "wbtc-usdc-860",
            "amount": "6200",
            "collateral_amount": "0.1",
            "debt_amount": "6200",
            "ltv": "0.75",
            "numeraire": "USDC",
        },
    )

    recommendation = body["recommendation"]
    assert recommendation["decision"] == "UNAVAILABLE"
    assert recommendation["confidence"] == "unavailable"
    assert "live oracle 0x" in recommendation["explanation"]
    assert "a current exit estimate" in recommendation["explanation"]
    assert body["exit_estimate"]["freshness"] == "expired"
    assert any("still expired" in warning for warning in body["exit_estimate"]["warnings"])
    oracle = next(node for node in body["graph"]["nodes"] if node["node_type"] == "oracle")
    assert oracle["freshness"] == "expired"
    assert DEGRADED_MARKET_ID in {node["node_id"].split(":")[-1] for node in body["graph"]["nodes"]}
