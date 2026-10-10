from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    AnalysisRequest,
    CatalogEntry,
    ExitEstimate,
    ProbabilityEstimate,
    RiskNode,
)


NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def lender_request() -> dict:
    return {
        "position": {
            "mode": "lender",
            "entity_id": "belem-rwa-eurcv",
            "amount": "1000",
            "numeraire": "EURCV",
        },
        "scenarios": [{"scenario_id": "stablecoin-depeg", "severity": "0.10"}],
    }


def borrower_request() -> dict:
    return {
        "position": {
            "mode": "borrower",
            "entity_id": "weth-usdc-860",
            "amount": "3000",
            "collateral_amount": "2.5",
            "debt_amount": "3000",
            "ltv": "0.60",
            "numeraire": "USDC",
        },
        "scenarios": [{"scenario_id": "collateral-shock", "severity": "0.15"}],
    }


def test_analysis_request_models_accept_equivalent_lender_and_borrower_shapes():
    assert AnalysisRequest.model_validate(lender_request()).position.mode == "lender"
    assert AnalysisRequest.model_validate(borrower_request()).position.mode == "borrower"


@pytest.mark.parametrize("field", ["collateral_amount", "debt_amount", "ltv"])
def test_analysis_request_model_forbids_borrower_fields_for_lenders(field):
    payload = lender_request()
    payload["position"][field] = "1"

    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["collateral_amount", "debt_amount", "ltv"])
def test_analysis_request_model_requires_borrower_fields(field):
    payload = borrower_request()
    del payload["position"][field]

    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(payload)


def test_analysis_request_model_requires_at_least_one_scenario():
    payload = lender_request()
    payload["scenarios"] = []

    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(payload)


def test_probability_estimate_requires_fixed_horizon_and_provenance():
    payload = {
        "method": "empirical_rolling_window",
        "low": "0.01",
        "central": "0.02",
        "high": "0.03",
        "confidence": "high",
        "horizon_days": 30,
        "source": "chainlink:ETH/USD",
        "retrieved_at": NOW,
        "freshness": "fresh",
        "explanation": "Observed in rolling windows.",
    }
    assert ProbabilityEstimate.model_validate(payload).horizon_days == 30

    payload["horizon_days"] = 31
    with pytest.raises(ValidationError):
        ProbabilityEstimate.model_validate(payload)


def test_exit_estimate_requires_quote_timestamp_and_provenance():
    payload = {
        "withdrawable": True,
        "route": [],
        "slippage": "0",
        "fees": "0",
        "gas": "1",
        "price_impact": "0",
        "total_cost": "1",
        "quote_age_seconds": 5,
        "quoted_at": NOW,
        "source": "uniswap:onchain",
        "retrieved_at": NOW,
        "freshness": "fresh",
        "warnings": [],
    }
    assert ExitEstimate.model_validate(payload).quoted_at == NOW

    del payload["quoted_at"]
    with pytest.raises(ValidationError):
        ExitEstimate.model_validate(payload)


def test_risk_node_rejects_binary_float_metrics():
    payload = {
        "node_id": "market:1:weth-usdc-860",
        "node_type": "market",
        "label": "WETH / USDC",
        "metrics": {"total_supply": Decimal("1000000.25")},
        "provenance": "morpho_api",
        "freshness": "fresh",
        "observed_at": NOW,
    }
    assert RiskNode.model_validate(payload).metrics["total_supply"] == Decimal(
        "1000000.25"
    )

    payload["metrics"]["total_supply"] = 0.1
    with pytest.raises(ValidationError):
        RiskNode.model_validate(payload)


def valid_catalog_entry() -> dict:
    return {
        "entity_id": "weth-usdc-860",
        "chain_id": 1,
        "entity_type": "morpho_market",
        "address_or_market_id": (
            "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"
        ),
        "name": "WETH collateral / USDC debt",
        "description": "Representative stablecoin borrowing market.",
        "provenance": "representative_proxy",
        "provenance_label": "Representative stablecoin borrowing market",
        "supported": True,
        "supported_modes": ["lender", "borrower"],
        "lltv": "0.86",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_id", ""),
        ("address_or_market_id", "not-a-hex-identifier"),
        ("name", ""),
        ("description", ""),
        ("provenance_label", ""),
        ("supported_modes", []),
        ("supported_modes", ["lender", "lender"]),
        ("lltv", "1.01"),
    ],
)
def test_catalog_entry_matches_schema_constraints(field, value):
    payload = valid_catalog_entry()
    payload[field] = value

    with pytest.raises(ValidationError):
        CatalogEntry.model_validate(payload)
