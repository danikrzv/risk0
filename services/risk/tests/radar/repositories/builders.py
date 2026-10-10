from datetime import UTC, datetime
from decimal import Decimal

from app.radar.contracts import (
    HealthState,
    HealthStatus,
    Incident,
    IncidentImpact,
    IncidentSeverity,
    IncidentStatus,
    RadarEntity,
    RadarEntityType,
    StateSnapshot,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
CATALOG_VAULT = "vault:1:0x1111111111111111111111111111111111111111"
NEIGHBOR_VAULT = "vault:1:0x2222222222222222222222222222222222222222"
ORACLE = "oracle:1:0x4444444444444444444444444444444444444444"


def entity(
    entity_type: RadarEntityType = RadarEntityType.VAULT,
    key: str = "0x1111111111111111111111111111111111111111",
    chain_id: int = 1,
    **overrides,
) -> RadarEntity:
    values = {
        "entity_id": f"{entity_type.value}:{chain_id}:{key}",
        "entity_type": entity_type,
        "chain_id": chain_id,
        "address_or_key": key,
        "label": f"{entity_type.value} {key[:6]}",
        "source": "fixture:morpho",
    }
    values.update(overrides)
    return RadarEntity(**values)


def snapshot(
    entity_id: str = CATALOG_VAULT,
    observed_at: datetime = NOW,
    *,
    metrics: dict | None = None,
    source_identity: str = "morpho:1",
    **overrides,
) -> StateSnapshot:
    values = {
        "snapshot_id": f"{entity_id}|{source_identity}|{observed_at.isoformat()}",
        "entity_id": entity_id,
        "state_type": RadarEntityType(entity_id.split(":", 1)[0]),
        "observed_at": observed_at,
        "retrieved_at": observed_at,
        "source": "https://api.morpho.org/graphql",
        "source_identity": source_identity,
        "freshness": "fresh",
        "metrics": metrics if metrics is not None else {"utilization": Decimal("0.5")},
    }
    values.update(overrides)
    return StateSnapshot(**values)


def health(
    entity_id: str = CATALOG_VAULT,
    evaluated_at: datetime = NOW,
    status: HealthStatus = HealthStatus.HEALTHY,
) -> HealthState:
    return HealthState(
        entity_id=entity_id,
        status=status,
        score=Decimal("0.9") if status is not HealthStatus.UNKNOWN else None,
        reasons=(f"{status.value.lower()} fixture",),
        missing_fields=() if status is not HealthStatus.UNKNOWN else ("utilization",),
        evaluated_at=evaluated_at,
        rule_version="health-v1",
    )


def incident(
    incident_id: str = "incident-1",
    *,
    source_entity_id: str = ORACLE,
    severity: IncidentSeverity = IncidentSeverity.CRITICAL,
    first_observed_at: datetime = NOW,
    last_observed_at: datetime | None = None,
    metrics: dict | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        source_entity_id=source_entity_id,
        severity=severity,
        status=IncidentStatus.OPEN,
        trigger_metrics=metrics if metrics is not None else {"deviation": Decimal("0.08")},
        rule_version="oracle-deviation-v1",
        first_observed_at=first_observed_at,
        last_observed_at=last_observed_at or first_observed_at,
        reasons=("oracle deviates from consensus",),
    )


def impact(
    incident_id: str = "incident-1",
    target_entity_id: str = CATALOG_VAULT,
    *,
    amount: Decimal | None = Decimal("1250.50"),
) -> IncidentImpact:
    return IncidentImpact(
        incident_id=incident_id,
        target_entity_id=target_entity_id,
        path=(ORACLE, target_entity_id),
        distance=1,
        confidence=Decimal("0.9"),
        estimated_affected_amount=amount,
        reasons=("allocates to affected market",),
        revealed=False,
    )
