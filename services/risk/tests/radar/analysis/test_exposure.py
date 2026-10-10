from datetime import UTC, datetime
from decimal import Decimal

from app.radar.analysis.exposure import ExposureImpactEstimator
from app.radar.contracts import (
    Incident,
    IncidentImpact,
    IncidentSeverity,
    IncidentStatus,
    ManualExposure,
    RadarRelationship,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
VAULT = "vault:1:vault"
MARKET = "market:1:market"
ORACLE = "oracle:1:oracle"


def incident(source: str = ORACLE) -> Incident:
    return Incident(
        incident_id="incident:test",
        source_entity_id=source,
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.OPEN,
        trigger_metrics={},
        rule_version="test-v1",
        first_observed_at=NOW,
        last_observed_at=NOW,
        reasons=("test",),
    )


def impact() -> IncidentImpact:
    return IncidentImpact(
        incident_id="incident:test",
        target_entity_id=VAULT,
        path=(ORACLE, MARKET, VAULT),
        distance=2,
        confidence=Decimal("0.90"),
        estimated_affected_amount=None,
        reasons=("propagated",),
        revealed=False,
    )


def allocation(weight: str) -> RadarRelationship:
    return RadarRelationship(
        source_entity_id=VAULT,
        target_entity_id=MARKET,
        relationship_type="allocated_to",
        weight=Decimal(weight),
        metadata={},
    )


def test_lender_impact_is_weighted_by_allocation_on_the_path():
    exposure = ManualExposure(
        exposure_id="exposure-1",
        entity_id=VAULT,
        mode="lender",
        amount=Decimal("1000"),
        numeraire="USDC",
        created_at=NOW,
    )

    result = ExposureImpactEstimator().estimate(
        exposure, incident(), (impact(),), (allocation("0.40"),)
    )

    assert result.impacts[0].estimated_affected_amount == Decimal("400")


def test_borrower_impact_uses_debt_and_reports_ltv_context():
    exposure = ManualExposure(
        exposure_id="exposure-2",
        entity_id=MARKET,
        mode="borrower",
        amount=Decimal("1500"),
        numeraire="USDC",
        collateral_amount=Decimal("2000"),
        debt_amount=Decimal("1000"),
        ltv=Decimal("0.50"),
        created_at=NOW,
    )
    market_impact = impact().model_copy(
        update={"target_entity_id": MARKET, "path": (ORACLE, MARKET), "distance": 1}
    )

    result = ExposureImpactEstimator().estimate(exposure, incident(), (market_impact,))

    assert result.impacts[0].estimated_affected_amount == Decimal("1000")
    assert "borrower LTV 0.50" in result.impacts[0].reasons


def test_chain_only_incident_does_not_invent_monetary_impact():
    exposure = ManualExposure(
        exposure_id="exposure-1",
        entity_id=VAULT,
        mode="lender",
        amount=Decimal("1000"),
        numeraire="USDC",
        created_at=NOW,
    )
    chain_incident = incident("chain:1:ethereum")

    result = ExposureImpactEstimator().estimate(
        exposure, chain_incident, (impact(),), (allocation("0.40"),)
    )

    assert result.impacts[0].estimated_affected_amount is None
    assert "monetary impact unavailable for chain-only incident" in result.impacts[0].reasons


def test_unreached_exposure_has_no_impact():
    exposure = ManualExposure(
        exposure_id="exposure-1",
        entity_id="vault:1:other",
        mode="lender",
        amount=Decimal("1000"),
        numeraire="USDC",
        created_at=NOW,
    )

    result = ExposureImpactEstimator().estimate(exposure, incident(), (impact(),))

    assert result.impacts == ()
