from decimal import Decimal

from app.contracts.models import PositionMode
from app.radar.contracts import (
    ExposureImpactResponse,
    Incident,
    IncidentImpact,
    ManualExposure,
    RadarRelationship,
    RadarRelationshipType,
)


class ExposureImpactEstimator:
    def estimate(
        self,
        exposure: ManualExposure,
        incident: Incident,
        impacts: tuple[IncidentImpact, ...],
        relationships: tuple[RadarRelationship, ...] = (),
    ) -> ExposureImpactResponse:
        candidates = tuple(
            impact
            for impact in impacts
            if impact.incident_id == incident.incident_id
            and (
                impact.target_entity_id == exposure.entity_id
                or exposure.entity_id in impact.path
            )
        )
        if not candidates:
            return ExposureImpactResponse(
                exposure=exposure,
                impacts=(),
                generated_at=incident.last_observed_at,
            )
        source_type = incident.source_entity_id.split(":", 1)[0]
        result: list[IncidentImpact] = []
        for impact in sorted(candidates, key=lambda item: (item.distance, item.target_entity_id)):
            reasons = list(impact.reasons)
            amount: Decimal | None
            if source_type == "chain":
                amount = None
                reasons.append("monetary impact unavailable for chain-only incident")
            elif exposure.mode is PositionMode.BORROWER:
                amount = exposure.debt_amount
                reasons.append(f"borrower LTV {exposure.ltv}")
                reasons.append(
                    f"collateral {exposure.collateral_amount} / debt {exposure.debt_amount} {exposure.numeraire}"
                )
            else:
                weight = self._path_weight(impact.path, relationships)
                amount = exposure.amount * weight
                reasons.append(f"lender path allocation weight {weight}")
            result.append(
                impact.model_copy(
                    update={
                        "estimated_affected_amount": amount,
                        "reasons": tuple(reasons),
                    }
                )
            )
            break
        return ExposureImpactResponse(
            exposure=exposure,
            impacts=tuple(result),
            generated_at=incident.last_observed_at,
        )

    @staticmethod
    def _path_weight(
        path: tuple[str, ...], relationships: tuple[RadarRelationship, ...]
    ) -> Decimal:
        by_pair = {
            frozenset((relationship.source_entity_id, relationship.target_entity_id)): relationship
            for relationship in relationships
        }
        weight = Decimal(1)
        for source, target in zip(path, path[1:]):
            relationship = by_pair.get(frozenset((source, target)))
            if (
                relationship is not None
                and relationship.relationship_type is RadarRelationshipType.ALLOCATED_TO
                and relationship.weight is not None
            ):
                weight *= relationship.weight
        return weight
