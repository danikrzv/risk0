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


@pytest.mark.parametrize(
    "payload",
    [
        {
            "position": {
                "mode": "lender",
                "entity_id": "belem-rwa-eurcv",
                "amount": "1000",
                "numeraire": "EURCV",
            },
            "scenarios": [
                {"scenario_id": "stablecoin-depeg", "severity": "0.10"}
            ],
        },
        {
            "position": {
                "mode": "borrower",
                "entity_id": "weth-usdc-860",
                "amount": "3000",
                "collateral_amount": "2.5",
                "debt_amount": "3000",
                "ltv": "0.60",
                "numeraire": "USDC",
            },
            "scenarios": [
                {"scenario_id": "collateral-shock", "severity": "0.15"}
            ],
        },
    ],
    ids=["lender", "borrower"],
)
def test_analysis_request_schema_accepts_supported_positions(payload):
    schema = load_schema("analysis-request.schema.json")
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(payload)


def test_analysis_response_schema_rejects_unknown_recommendation():
    schema = load_schema("analysis-response.schema.json")
    Draft202012Validator.check_schema(schema)
    response = {
        "graph": {
            "nodes": [],
            "edges": [],
            "generated_at": "2026-10-07T12:00:00Z",
            "completeness_warnings": [],
        },
        "scenario_results": [],
        "exit_estimate": {
            "withdrawable": True,
            "route": [],
            "slippage": "0",
            "fees": "0",
            "gas": "1",
            "price_impact": "0",
            "total_cost": "1",
            "quote_age_seconds": 5,
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
    validator = Draft202012Validator(schema)
    validator.validate(response)

    response["recommendation"]["decision"] = "HOLD"

    with pytest.raises(ValidationError):
        validator.validate(response)
