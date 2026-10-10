from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = ROOT / "packages" / "contracts" / "schemas" / "radar.schema.json"
FIXTURES = ROOT / "services" / "risk" / "tests" / "fixtures" / "radar"
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def contracts():
    try:
        return importlib.import_module("app.radar.contracts")
    except ModuleNotFoundError as error:
        pytest.fail(f"radar contracts do not exist: {error}")


@pytest.fixture(scope="module")
def schema() -> dict:
    if not SCHEMA_PATH.exists():
        pytest.fail(f"radar schema does not exist: {SCHEMA_PATH}")
    loaded = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(loaded)
    return loaded


def definition_validator(schema: dict, definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )


def entity_payload(entity_id: str = "vault:1:0x1111") -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "vault",
        "chain_id": 1,
        "address_or_key": entity_id.rsplit(":", 1)[-1],
        "label": "Catalog Vault",
        "source": "fixture:morpho",
        "catalog": True,
        "visible": True,
        "metadata": {},
    }


def snapshot_payload() -> dict:
    return {
        "snapshot_id": "snapshot:vault:1:0x1111:2026-10-10T12:00:00Z",
        "entity_id": "vault:1:0x1111",
        "state_type": "vault",
        "observed_at": NOW,
        "retrieved_at": NOW,
        "source": "morpho_api",
        "source_identity": "vault:1:0x1111",
        "freshness": "fresh",
        "metrics": {"total_assets_usd": Decimal("1000000.25")},
        "missing_fields": [],
    }


def test_radar_enums_match_the_frozen_schema_exactly(schema, contracts):
    expected = {
        "HealthStatus": ["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"],
        "RadarEntityType": [
            "chain",
            "protocol",
            "vault",
            "market",
            "asset",
            "oracle",
            "irm",
            "position",
        ],
        "IncidentSeverity": ["WARNING", "CRITICAL"],
        "IncidentStatus": ["OPEN", "RESOLVED"],
        "RadarRelationshipType": [
            "allocated_to",
            "lends",
            "borrows",
            "uses_collateral",
            "uses_oracle",
            "uses_irm",
            "backed_by",
            "deployed_on",
            "exits_through",
            "similar_to",
        ],
    }

    for enum_name, values in expected.items():
        assert schema["$defs"][enum_name]["enum"] == values
        assert [item.value for item in getattr(contracts, enum_name)] == values


def test_similarity_policy_uses_decimal_strings(schema, contracts):
    policy = schema["$defs"]["SimilarityPolicy"]

    assert policy["properties"]["storage_threshold"]["const"] == "0.33"
    assert policy["properties"]["visibility_threshold"]["const"] == "0.60"
    assert policy["properties"]["visible_limit"]["const"] == 10
    assert contracts.SIMILARITY_STORAGE_THRESHOLD == Decimal("0.33")
    assert contracts.SIMILARITY_VISIBILITY_THRESHOLD == Decimal("0.60")
    assert contracts.SIMILARITY_VISIBLE_LIMIT == 10


def test_contract_models_are_frozen_and_forbid_extra_fields(contracts):
    entity = contracts.RadarEntity.model_validate(entity_payload())

    with pytest.raises(ValidationError):
        contracts.RadarEntity.model_validate({**entity_payload(), "secret": "nope"})
    with pytest.raises(ValidationError):
        entity.label = "Mutated"


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 10, 10, 12),
        datetime(2026, 10, 10, 14, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_contract_timestamps_require_timezone_aware_utc(contracts, timestamp):
    payload = snapshot_payload()
    payload["observed_at"] = timestamp

    with pytest.raises(ValidationError):
        contracts.StateSnapshot.model_validate(payload)


def test_decimal_fields_reject_binary_floats_and_serialize_as_strings(contracts):
    snapshot = contracts.StateSnapshot.model_validate(snapshot_payload())
    assert snapshot.model_dump(mode="json")["metrics"]["total_assets_usd"] == (
        "1000000.25"
    )

    invalid = snapshot_payload()
    invalid["metrics"]["total_assets_usd"] = 0.1
    with pytest.raises(ValidationError):
        contracts.StateSnapshot.model_validate(invalid)


def test_graph_response_rejects_duplicate_entity_ids(contracts):
    entity = entity_payload()
    payload = {
        "schema_version": "1.0.0",
        "entities": [entity, entity],
        "relationships": [],
        "similarities": [],
        "active_incidents": [],
        "generated_at": NOW,
    }

    with pytest.raises(ValidationError):
        contracts.RadarGraphResponse.model_validate(payload)


def test_stable_entity_ids_match_type_chain_and_address(contracts):
    invalid = entity_payload("market:1:0x1111")
    invalid["entity_type"] = "vault"

    with pytest.raises(ValidationError):
        contracts.RadarEntity.model_validate(invalid)


@pytest.mark.parametrize(
    ("fixture_name", "definition"),
    [
        ("monitoring-cycle.json", "RadarGraphResponse"),
        ("oracle-incident.json", "IncidentGraphResponse"),
    ],
)
def test_deterministic_fixtures_validate_against_schema(
    schema, contracts, fixture_name, definition
):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    definition_validator(schema, definition).validate(payload)
    getattr(contracts, definition).model_validate(payload)


def test_healthy_fixture_contains_one_catalog_vault_and_one_visible_neighbor():
    payload = json.loads((FIXTURES / "monitoring-cycle.json").read_text())

    assert len(payload["entities"]) == 2
    assert sum(entity["catalog"] for entity in payload["entities"]) == 1
    assert all(entity["visible"] for entity in payload["entities"])
    assert payload["active_incidents"] == []


def test_incident_fixture_contains_the_propagated_hidden_neighbor_case():
    payload = json.loads((FIXTURES / "oracle-incident.json").read_text())
    entity_types = [entity["entity_type"] for entity in payload["entities"]]

    assert entity_types == ["oracle", "market", "vault", "vault", "position"]
    hidden_neighbor = payload["entities"][3]
    assert hidden_neighbor["catalog"] is False
    assert hidden_neighbor["visible"] is True
    assert payload["similarities"][0]["score"] == "0.48"
    assert payload["impacts"][0]["revealed"] is True
    assert payload["incident"]["severity"] == "CRITICAL"


def test_schema_rejects_non_utc_timestamp_and_numeric_decimal(schema):
    snapshot = snapshot_payload()
    snapshot["observed_at"] = "2026-10-10T14:00:00+02:00"
    snapshot["retrieved_at"] = "2026-10-10T12:00:00Z"
    snapshot["metrics"]["total_assets_usd"] = "1000000.25"
    validator = definition_validator(schema, "StateSnapshot")

    with pytest.raises(SchemaValidationError):
        validator.validate(snapshot)

    snapshot["observed_at"] = "2026-10-10T12:00:00Z"
    snapshot["metrics"]["total_assets_usd"] = 1000000.25
    with pytest.raises(SchemaValidationError):
        validator.validate(snapshot)
