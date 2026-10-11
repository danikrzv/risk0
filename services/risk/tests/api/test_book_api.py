"""`POST /v1/book`: contract, validation, sanitized failures, and a fixture-mode run."""

import json
import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app.api.analysis import get_analysis_service
from app.main import app


ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = ROOT / "packages" / "contracts" / "schemas"
SCENARIOS = [
    {"scenario_id": "stablecoin-depeg", "severity": "0.10"},
    {"scenario_id": "collateral-shock", "severity": "0.15"},
    {"scenario_id": "oracle-failure", "severity": "0.05"},
    {"scenario_id": "liquidity-collapse", "severity": "0.80"},
    {"scenario_id": "protocol-impairment", "severity": "0.10"},
]


def validator(definition: str) -> Draft202012Validator:
    schemas = [json.loads(path.read_text()) for path in SCHEMAS.glob("*.schema.json")]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas if "$id" in schema
    )
    return Draft202012Validator(
        {"$ref": f"https://risk0.local/schemas/book.schema.json#/$defs/{definition}"},
        registry=registry,
    )


class RaisingService:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        raise RuntimeError("provider body from https://rpc.example/secret")


@pytest.fixture
def api():
    def use(service):
        app.dependency_overrides[get_analysis_service] = lambda: service
        return TestClient(app, raise_server_exceptions=False)

    yield use
    app.dependency_overrides.clear()


def lender(entity_id: str, amount: str, numeraire: str) -> dict:
    return {"mode": "lender", "entity_id": entity_id, "amount": amount, "numeraire": numeraire}


def test_request_schema_accepts_the_brief_book_and_rejects_bad_weights():
    request_validator = validator("BookRequest")
    payload = {
        "pockets": [
            {"position": lender("belem-rwa-eurcv", "100000", "EURCV"), "weight": "0.1"}
            for _ in range(10)
        ],
        "scenarios": SCENARIOS,
    }
    request_validator.validate(payload)
    for weight in ("0", "0.0", "1.5", "-0.1", "abc"):
        payload["pockets"][0]["weight"] = weight
        assert not request_validator.is_valid(payload), weight
    payload["pockets"][0]["weight"] = "0.1"
    assert not request_validator.is_valid({**payload, "book_value": "1000"})
    assert request_validator.is_valid({**payload, "book_value": "1000", "numeraire": "USD"})


def test_every_failing_pocket_is_unavailable_and_sanitized(api, caplog):
    service = RaisingService()
    client = api(service)
    payload = {
        "pockets": [
            {"position": lender("belem-rwa-eurcv", "1000", "EURCV"), "weight": "0.5"},
            {"position": lender("mev-capital-weth", "1", "WETH"), "weight": "0.5"},
        ],
        "scenarios": SCENARIOS,
    }

    with caplog.at_level(logging.DEBUG):
        response = client.post("/v1/book", json=payload)

    assert response.status_code == 200
    body = response.json()
    validator("BookResponse").validate(body)
    assert service.calls == 2
    assert {pocket["decision"] for pocket in body["pockets"]} == {"UNAVAILABLE"}
    assert body["totals"]["complete"] is False
    assert "secret" not in response.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"pockets": [{"position": lender("a", "1", "USDC"), "weight": "0.7"}] * 2, "scenarios": SCENARIOS},
        {"pockets": [{"position": lender("a", "1", "USDC"), "weight": "0.5"}]},
        {"pockets": [], "scenarios": SCENARIOS},
        {
            "pockets": [{"position": lender("a", "1", "USDC"), "weight": "0.5"}],
            "scenarios": [{"scenario_id": "collateral-shock", "severity": "1.5"}],
        },
    ],
    ids=["weights-above-one", "no-scenarios", "no-pockets", "severity-out-of-range"],
)
def test_invalid_books_are_rejected_with_422_before_any_analysis(api, payload):
    service = RaisingService()
    response = api(service).post("/v1/book", json=payload)

    assert response.status_code == 422
    assert service.calls == 0


def test_missing_analysis_service_is_a_sanitized_503(api):
    response = api(None).post(
        "/v1/book",
        json={"pockets": [{"position": lender("a", "1", "USDC"), "weight": "1"}], "scenarios": SCENARIOS},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "The risk analysis is temporarily unavailable."}


def test_fixture_mode_book_runs_each_pocket_through_the_analysis_service(api, monkeypatch, tmp_path):
    monkeypatch.setenv("RISK0_FIXTURE_CACHE_PATH", str(tmp_path / "fixture.sqlite3"))

    def no_network(*args, **kwargs):
        raise AssertionError("fixture mode must not open a network connection")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    from app.fixture_mode import build_fixture_analysis_service

    client = api(build_fixture_analysis_service())
    payload = {
        "pockets": [
            {"pocket_id": "eurcv", "position": lender("belem-rwa-eurcv", "100000", "EURCV"), "weight": "0.1"},
            {"pocket_id": "wsteth", "position": lender("wsteth-weth-965", "100", "WETH"), "weight": "0.1"},
            {"pocket_id": "usdc", "position": lender("weth-usdc-860", "100000", "USDC"), "weight": "0.1"},
            {"pocket_id": "unknown", "position": lender("not-in-catalog", "1", "USDC"), "weight": "0.1"},
        ],
        "scenarios": SCENARIOS,
    }

    response = client.post("/v1/book", json=payload)

    assert response.status_code == 200
    body = response.json()
    validator("BookResponse").validate(body)
    pockets = {pocket["pocket_id"]: pocket for pocket in body["pockets"]}
    assert pockets["unknown"]["decision"] == "UNAVAILABLE"
    assert pockets["unknown"]["rank"] == 4
    assert "not in the curated catalog" in pockets["unknown"]["unavailable_reasons"][0]
    for pocket_id in ("eurcv", "wsteth", "usdc"):
        pocket = pockets[pocket_id]
        assert pocket["decision"] in {"STAY", "EXIT"}
        assert pocket["break_even"] is not None
        expected = Decimal(pocket["expected_loss"]["amount"]) / Decimal(pocket["pocket_value"])
        assert Decimal(pocket["expected_loss"]["fraction_of_pocket"]) == expected.quantize(Decimal("1E-18"))
    ranked = [pocket for pocket in body["pockets"] if pocket["decision"] != "UNAVAILABLE"]
    advantages = [Decimal(pocket["exit_advantage"]["fraction_of_book"]) for pocket in ranked]
    assert advantages == sorted(advantages, reverse=True)
    assert body["totals"]["allocated_weight"] == "0.4"
    assert body["totals"]["unavailable_count"] == 1
