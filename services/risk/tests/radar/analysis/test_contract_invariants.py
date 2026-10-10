import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.radar.contracts import (
    Incident,
    IncidentImpact,
    ManualExposure,
    SimilarityRecord,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
FIXTURES = Path(__file__).parents[2] / "fixtures" / "radar"
BINARY_COMPONENTS = {"same_chain", "same_curator"}
COMPONENT_WEIGHTS = {
    "shared_markets": Decimal("0.35"),
    "asset_overlap": Decimal("0.20"),
    "oracle_overlap": Decimal("0.15"),
    "irm_match": Decimal("0.10"),
    "lltv_proximity": Decimal("0.10"),
    "same_chain": Decimal("0.05"),
    "same_curator": Decimal("0.05"),
}


@pytest.mark.parametrize("name", ["monitoring-cycle.json", "oracle-incident.json"])
def test_fixture_similarity_components_respect_the_weights(name):
    payload = json.loads((FIXTURES / name).read_text())
    for record in payload["similarities"]:
        components = {key: Decimal(value) for key, value in record["components"].items()}
        assert sum(components.values(), Decimal(0)) == Decimal(record["score"])
        for key, value in components.items():
            assert Decimal(0) <= value <= COMPONENT_WEIGHTS[key]
            if key in BINARY_COMPONENTS:
                assert value in (Decimal(0), COMPONENT_WEIGHTS[key])
        assert not set(record["missing_dimensions"]) & {
            key for key, value in components.items() if value > 0
        }


def incident(**overrides) -> dict:
    values = {
        "incident_id": "incident:test",
        "source_entity_id": "oracle:1:test",
        "severity": "CRITICAL",
        "status": "OPEN",
        "trigger_metrics": {},
        "rule_version": "test-v1",
        "first_observed_at": NOW,
        "last_observed_at": NOW,
        "reasons": ("test",),
    }
    values.update(overrides)
    return values


def test_incident_timestamps_are_ordered():
    with pytest.raises(ValidationError):
        Incident(**incident(last_observed_at=NOW - timedelta(minutes=1)))
    with pytest.raises(ValidationError):
        Incident(**incident(status="RESOLVED", resolved_at=NOW - timedelta(minutes=1)))
    with pytest.raises(ValidationError):
        Incident(**incident(resolved_at=NOW))
    with pytest.raises(ValidationError):
        Incident(**incident(status="RESOLVED"))
    assert Incident(**incident(status="RESOLVED", resolved_at=NOW)).resolved_at == NOW


def impact(**overrides) -> dict:
    values = {
        "incident_id": "incident:test",
        "target_entity_id": "market:1:m",
        "path": ("oracle:1:o", "market:1:m"),
        "distance": 1,
        "confidence": Decimal("1"),
        "estimated_affected_amount": None,
        "reasons": ("test",),
        "revealed": False,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        {"distance": 2},
        {"target_entity_id": "vault:1:v"},
        {"path": ("oracle:1:o", "market:1:m", "market:1:m"), "distance": 2},
    ],
)
def test_impact_path_must_match_target_and_distance(overrides):
    with pytest.raises(ValidationError):
        IncidentImpact(**impact(**overrides))


def test_similarity_record_rejects_self_pairs_and_visible_unstored():
    base = {
        "catalog_entity_id": "vault:1:a",
        "candidate_entity_id": "vault:1:b",
        "score": Decimal("0.70"),
        "components": {},
        "stored": True,
        "normally_visible": True,
        "computed_at": NOW,
    }
    SimilarityRecord(**base)
    with pytest.raises(ValidationError):
        SimilarityRecord(**{**base, "candidate_entity_id": "vault:1:a"})
    with pytest.raises(ValidationError):
        SimilarityRecord(**{**base, "stored": False})


def test_exposure_mode_field_rules():
    common = {
        "exposure_id": "exposure-1",
        "entity_id": "vault:1:v",
        "amount": Decimal("1"),
        "numeraire": "USDC",
        "created_at": NOW,
    }
    with pytest.raises(ValidationError):
        ManualExposure(**common, mode="lender", ltv=Decimal("0.5"))
    with pytest.raises(ValidationError):
        ManualExposure(**common, mode="borrower", debt_amount=Decimal("1"))
    with pytest.raises(ValidationError):
        ManualExposure(**{**common, "created_at": NOW.replace(tzinfo=None)}, mode="lender")
