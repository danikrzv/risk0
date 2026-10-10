import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = ROOT / "packages" / "contracts" / "schemas"


def load_schema(name: str) -> dict:
    with (SCHEMAS / name).open() as schema_file:
        return json.load(schema_file)


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


def valid_response() -> dict:
    return {
        "graph": {
            "nodes": [
                {
                    "node_id": "market:1:weth-usdc-860",
                    "node_type": "market",
                    "label": "WETH / USDC",
                    "metrics": {"total_supply": "1000000.25", "active": True},
                    "provenance": "morpho_api",
                    "freshness": "fresh",
                    "observed_at": "2026-10-07T12:00:00Z",
                }
            ],
            "edges": [],
            "generated_at": "2026-10-07T12:00:00Z",
            "completeness_warnings": [],
        },
        "scenario_results": [
            {
                "scenario_id": "collateral-shock",
                "affected_path": ["asset:1:weth", "market:1:weth-usdc-860"],
                "stressed_values": {"stressed_ltv": "0.70"},
                "hard_triggers": [],
                "impact": "25",
                "impact_components": {"collateral_loss": "25"},
                "probability": {
                    "method": "empirical_rolling_window",
                    "low": "0.01",
                    "central": "0.02",
                    "high": "0.03",
                    "confidence": "high",
                    "horizon_days": 30,
                    "source": "chainlink:ETH/USD",
                    "retrieved_at": "2026-10-07T12:00:00Z",
                    "freshness": "fresh",
                    "explanation": "Observed in rolling historical windows.",
                },
                "confidence": "high",
                "explanation": "The position remains below its LLTV.",
            }
        ],
        "exit_estimate": {
            "withdrawable": True,
            "route": [],
            "slippage": "0",
            "fees": "0",
            "gas": "1",
            "price_impact": "0",
            "total_cost": "1",
            "quote_age_seconds": 5,
            "quoted_at": "2026-10-07T12:00:00Z",
            "source": "uniswap:onchain",
            "retrieved_at": "2026-10-07T12:00:00Z",
            "freshness": "fresh",
            "warnings": [],
        },
        "recommendation": {
            "decision": "STAY",
            "expected_loss": "2",
            "exit_cost": "1",
            "triggers": [],
            "dominant_dependency": None,
            "confidence": "high",
            "explanation": "Expected loss remains below the cost of exiting.",
        },
        "analyzed_at": "2026-10-07T12:00:00Z",
    }


@pytest.mark.parametrize(
    "payload",
    [lender_request(), borrower_request()],
    ids=["lender", "borrower"],
)
def test_analysis_request_schema_accepts_supported_positions(payload):
    schema = load_schema("analysis-request.schema.json")
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("field", ["collateral_amount", "debt_amount", "ltv"])
def test_analysis_request_schema_forbids_borrower_fields_for_lenders(field):
    payload = lender_request()
    payload["position"][field] = "1"

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("analysis-request.schema.json")).validate(payload)


@pytest.mark.parametrize("field", ["collateral_amount", "debt_amount", "ltv"])
def test_analysis_request_schema_requires_borrower_fields(field):
    payload = borrower_request()
    del payload["position"][field]

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("analysis-request.schema.json")).validate(payload)


def test_analysis_request_schema_requires_at_least_one_scenario():
    payload = lender_request()
    payload["scenarios"] = []

    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("analysis-request.schema.json")).validate(payload)


def test_catalog_schema_requires_provenance_and_validates_catalog():
    schema = load_schema("catalog.schema.json")
    catalog = json.loads((ROOT / "data" / "catalog" / "ethereum.json").read_text())
    validator = Draft202012Validator(schema)
    validator.validate(catalog)

    for field in ("provenance", "provenance_label"):
        invalid = copy.deepcopy(catalog)
        del invalid["entries"][0][field]
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_analysis_response_schema_rejects_unknown_recommendation():
    validator = Draft202012Validator(load_schema("analysis-response.schema.json"))
    response = valid_response()
    validator.validate(response)

    response["recommendation"]["decision"] = "HOLD"

    with pytest.raises(ValidationError):
        validator.validate(response)


def test_analysis_response_schema_fixes_probability_horizon_at_30_days():
    validator = Draft202012Validator(load_schema("analysis-response.schema.json"))
    response = valid_response()
    validator.validate(response)
    response["scenario_results"][0]["probability"]["horizon_days"] = 31

    with pytest.raises(ValidationError):
        validator.validate(response)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("probability", "source"),
        ("probability", "retrieved_at"),
        ("probability", "freshness"),
        ("exit_estimate", "source"),
        ("exit_estimate", "retrieved_at"),
        ("exit_estimate", "freshness"),
        ("exit_estimate", "quoted_at"),
    ],
)
def test_analysis_response_schema_requires_external_data_provenance(section, field):
    validator = Draft202012Validator(load_schema("analysis-response.schema.json"))
    response = valid_response()
    validator.validate(response)
    target = (
        response["scenario_results"][0]["probability"]
        if section == "probability"
        else response[section]
    )
    del target[field]

    with pytest.raises(ValidationError):
        validator.validate(response)


@pytest.mark.parametrize(
    ("section", "field"),
    [("graph", "total_supply"), ("scenario", "stressed_ltv")],
)
def test_analysis_response_schema_rejects_json_numbers_for_domain_metrics(
    section, field
):
    validator = Draft202012Validator(load_schema("analysis-response.schema.json"))
    response = valid_response()
    validator.validate(response)
    metrics = (
        response["graph"]["nodes"][0]["metrics"]
        if section == "graph"
        else response["scenario_results"][0]["stressed_values"]
    )
    metrics[field] = 0.7

    with pytest.raises(ValidationError):
        validator.validate(response)
