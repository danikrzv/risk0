from dataclasses import dataclass
from datetime import datetime

from app.radar.contracts import (
    HealthState,
    HealthStatus,
    Incident,
    IncidentSeverity,
    IncidentStatus,
)


@dataclass(frozen=True, slots=True)
class IncidentChanges:
    opened: tuple[Incident, ...]
    updated: tuple[Incident, ...]
    resolved: tuple[Incident, ...]
    current: tuple[Incident, ...]


class IncidentDetector:
    def reconcile(
        self,
        previous: tuple[Incident, ...],
        health_states: tuple[HealthState, ...],
        now: datetime,
    ) -> IncidentChanges:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("incident reconciliation timestamp must be timezone-aware")
        previous_by_source = {item.source_entity_id: item for item in previous}
        current_by_source = dict(previous_by_source)
        opened: list[Incident] = []
        updated: list[Incident] = []
        resolved: list[Incident] = []

        for health in sorted(health_states, key=lambda item: item.entity_id):
            prior = previous_by_source.get(health.entity_id)
            if prior is not None and health.evaluated_at < prior.last_observed_at:
                continue
            if health.status is HealthStatus.HEALTHY:
                if prior is not None and prior.status is IncidentStatus.OPEN:
                    item = prior.model_copy(
                        update={
                            "status": IncidentStatus.RESOLVED,
                            "last_observed_at": health.evaluated_at,
                            "resolved_at": now,
                            "reasons": health.reasons,
                        }
                    )
                    current_by_source[health.entity_id] = item
                    resolved.append(item)
                continue

            severity = (
                IncidentSeverity.CRITICAL
                if health.status is HealthStatus.CRITICAL
                else IncidentSeverity.WARNING
            )
            trigger_metrics = {
                "health_status": health.status.value,
                "health_score": str(health.score) if health.score is not None else None,
            }
            if prior is None or prior.status is IncidentStatus.RESOLVED:
                item = Incident(
                    incident_id=self._incident_id(health, now),
                    source_entity_id=health.entity_id,
                    severity=severity,
                    status=IncidentStatus.OPEN,
                    trigger_metrics=trigger_metrics,
                    rule_version=health.rule_version,
                    first_observed_at=health.evaluated_at,
                    last_observed_at=health.evaluated_at,
                    reasons=health.reasons,
                )
                current_by_source[health.entity_id] = item
                opened.append(item)
                continue
            item = prior.model_copy(
                update={
                    "severity": severity,
                    "trigger_metrics": trigger_metrics,
                    "last_observed_at": health.evaluated_at,
                    "reasons": health.reasons,
                }
            )
            current_by_source[health.entity_id] = item
            updated.append(item)

        return IncidentChanges(
            opened=tuple(opened),
            updated=tuple(updated),
            resolved=tuple(resolved),
            current=tuple(sorted(current_by_source.values(), key=lambda item: item.incident_id)),
        )

    @staticmethod
    def _incident_id(health: HealthState, now: datetime) -> str:
        timestamp = now.isoformat().replace("+00:00", "Z")
        return f"incident:{health.entity_id}:{health.rule_version}:{timestamp}"
