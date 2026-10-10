from collections import defaultdict
from decimal import Decimal

from app.contracts.models import PositionMode
from app.radar.contracts import (
    ExposureImpactResponse,
    Incident,
    IncidentImpact,
    ManualExposure,
    RadarEntityType,
    RadarRelationship,
    RadarRelationshipType,
)


class ExposureImpactEstimator:
    """Attach one monetary estimate per (exposure, incident).

    The impact is matched, in order of preference, on the exposure's own graph
    node (``position:<chain>:<exposure_id>``), then on the entity it is held in,
    and finally on the incident source itself. A lender amount is the exposure
    amount times every allocation weight on the path; when a weight is not
    known the amount is reported unavailable rather than assumed to be 100%.
    """

    def estimate(
        self,
        exposure: ManualExposure,
        incident: Incident,
        impacts: tuple[IncidentImpact, ...],
        relationships: tuple[RadarRelationship, ...] = (),
    ) -> ExposureImpactResponse:
        impact = self._match(exposure, incident, impacts)
        if impact is None:
            return ExposureImpactResponse(
                exposure=exposure,
                impacts=(),
                generated_at=incident.last_observed_at,
            )
        reasons = list(impact.reasons)
        amount: Decimal | None
        if incident.source_entity_id.split(":", 1)[0] == RadarEntityType.CHAIN.value:
            amount = None
            reasons.append("monetary impact unavailable for chain-only incident")
        elif exposure.mode is PositionMode.BORROWER:
            amount = exposure.debt_amount
            reasons.append(f"borrower LTV {exposure.ltv}")
            reasons.append(
                f"collateral {exposure.collateral_amount} / debt {exposure.debt_amount} {exposure.numeraire}"
            )
        else:
            weight = _path_weight(impact.path, relationships)
            if weight is None:
                amount = None
                reasons.append("allocation weight on the path is unavailable")
            else:
                amount = exposure.amount * weight
                reasons.append(f"lender path allocation weight {weight}")
        return ExposureImpactResponse(
            exposure=exposure,
            impacts=(
                impact.model_copy(
                    update={"estimated_affected_amount": amount, "reasons": tuple(reasons)}
                ),
            ),
            generated_at=incident.last_observed_at,
        )

    @staticmethod
    def _match(
        exposure: ManualExposure,
        incident: Incident,
        impacts: tuple[IncidentImpact, ...],
    ) -> IncidentImpact | None:
        ranked: list[tuple[int, int, Decimal, str, IncidentImpact]] = []
        for impact in impacts:
            if impact.incident_id != incident.incident_id:
                continue
            if _is_exposure_node(impact.target_entity_id, exposure):
                rank = 0
            elif impact.target_entity_id == exposure.entity_id:
                rank = 1
            else:
                continue
            ranked.append(
                (rank, impact.distance, -impact.confidence, impact.target_entity_id, impact)
            )
        if ranked:
            return min(ranked, key=lambda item: item[:4])[4]
        if exposure.entity_id == incident.source_entity_id:
            return IncidentImpact(
                incident_id=incident.incident_id,
                target_entity_id=exposure.entity_id,
                path=(exposure.entity_id,),
                distance=0,
                confidence=Decimal(1),
                estimated_affected_amount=None,
                reasons=("exposure is held in the incident source",),
                revealed=False,
            )
        return None


def _is_exposure_node(entity_id: str, exposure: ManualExposure) -> bool:
    parts = entity_id.split(":", 2)
    return (
        len(parts) == 3
        and parts[0] == RadarEntityType.POSITION.value
        and parts[2] == exposure.exposure_id
    )


def _is_allocation_hop(dependent: str, dependency: str) -> bool:
    return dependent.startswith(f"{RadarEntityType.VAULT.value}:") and dependency.startswith(
        (f"{RadarEntityType.MARKET.value}:", f"{RadarEntityType.VAULT.value}:")
    )


def _path_weight(
    path: tuple[str, ...], relationships: tuple[RadarRelationship, ...]
) -> Decimal | None:
    """Product of allocation weights along a propagation path.

    Paths run from the incident source to the affected entity, i.e. against
    relationship direction, so hop ``(a, b)`` is the relationship ``b -> a``.
    Returns None when a vault allocation hop has no known weight.
    """
    if len(path) < 2:
        return Decimal(1)
    wanted = set(zip(path[1:], path))
    by_pair: dict[tuple[str, str], list[RadarRelationship]] = defaultdict(list)
    for relationship in relationships:
        pair = (relationship.source_entity_id, relationship.target_entity_id)
        if pair in wanted:
            by_pair[pair].append(relationship)
    weight = Decimal(1)
    for dependent, dependency in zip(path[1:], path):
        candidates = by_pair.get((dependent, dependency), ())
        weights = [
            item.weight
            for item in candidates
            if item.relationship_type is RadarRelationshipType.ALLOCATED_TO
            and item.weight is not None
        ]
        if weights:
            weight *= max(weights)
        elif _is_allocation_hop(dependent, dependency):
            return None
    return weight
