from dataclasses import dataclass
from collections.abc import Collection
from datetime import UTC, datetime

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


_SEVERITY_ORDER = {
    HealthStatus.HEALTHY: 0,
    HealthStatus.WARNING: 1,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.CRITICAL: 3,
}


class IncidentDetector:
    def reconcile(
        self,
        previous: tuple[Incident, ...],
        health_states: tuple[HealthState, ...],
        now: datetime,
        *,
        monitored_entity_ids: Collection[str] | None = None,
    ) -> IncidentChanges:
        """Open, update, or resolve one incident per source entity.

        Only observed deteriorations (WARNING/CRITICAL) open or update an
        incident. UNKNOWN health means missing, incomplete, or expired data: it
        never opens an incident and leaves an open one untouched, because losing
        visibility is evidence of neither deterioration nor improvement. Open
        incidents that were raised from such data gaps alone (by earlier rule
        versions) are resolved.

        Several health states for one entity in a batch collapse to the latest
        (ties: the most severe). When ``monitored_entity_ids`` is given, open
        incidents whose source is no longer monitored are resolved instead of
        being left open forever.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("incident reconciliation timestamp must be timezone-aware")
        current_by_source: dict[str, Incident] = {}
        for item in previous:
            kept = current_by_source.get(item.source_entity_id)
            # An open incident always takes precedence over resolved history.
            if kept is None or (
                kept.status is IncidentStatus.RESOLVED
                and (item.status is IncidentStatus.OPEN or item.last_observed_at > kept.last_observed_at)
            ):
                current_by_source[item.source_entity_id] = item
        latest: dict[str, HealthState] = {}
        for health in health_states:
            kept_health = latest.get(health.entity_id)
            if kept_health is None or (
                health.evaluated_at,
                _SEVERITY_ORDER[health.status],
            ) > (kept_health.evaluated_at, _SEVERITY_ORDER[kept_health.status]):
                latest[health.entity_id] = health
        opened: list[Incident] = []
        updated: list[Incident] = []
        resolved: list[Incident] = []

        for entity_id in sorted(latest):
            health = latest[entity_id]
            prior = current_by_source.get(entity_id)
            if prior is not None and health.evaluated_at < prior.last_observed_at:
                continue
            if health.status is HealthStatus.HEALTHY:
                if prior is not None and prior.status is IncidentStatus.OPEN:
                    item = prior.model_copy(
                        update={
                            "status": IncidentStatus.RESOLVED,
                            "last_observed_at": health.evaluated_at,
                            "resolved_at": max(now, health.evaluated_at),
                            "reasons": health.reasons,
                        }
                    )
                    current_by_source[entity_id] = item
                    resolved.append(item)
                continue
            if health.status is HealthStatus.UNKNOWN:
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
                    incident_id=self._incident_id(health),
                    source_entity_id=entity_id,
                    severity=severity,
                    status=IncidentStatus.OPEN,
                    trigger_metrics=trigger_metrics,
                    rule_version=health.rule_version,
                    first_observed_at=health.evaluated_at,
                    last_observed_at=health.evaluated_at,
                    reasons=health.reasons,
                )
                current_by_source[entity_id] = item
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
            current_by_source[entity_id] = item
            updated.append(item)

        for entity_id, item in sorted(current_by_source.items()):
            if item.status is IncidentStatus.OPEN and is_data_gap_incident(item):
                gap = item.model_copy(
                    update={
                        "status": IncidentStatus.RESOLVED,
                        "resolved_at": max(now, item.last_observed_at),
                    }
                )
                current_by_source[entity_id] = gap
                resolved.append(gap)

        if monitored_entity_ids is not None:
            monitored = set(monitored_entity_ids)
            for entity_id, item in sorted(current_by_source.items()):
                if item.status is not IncidentStatus.OPEN or entity_id in monitored:
                    continue
                orphan = item.model_copy(
                    update={
                        "status": IncidentStatus.RESOLVED,
                        "resolved_at": max(now, item.last_observed_at),
                        "reasons": ("source entity is no longer monitored",),
                    }
                )
                current_by_source[entity_id] = orphan
                resolved.append(orphan)

        return IncidentChanges(
            opened=tuple(opened),
            updated=tuple(updated),
            resolved=tuple(resolved),
            current=tuple(sorted(current_by_source.values(), key=lambda item: item.incident_id)),
        )

    @staticmethod
    def _incident_id(health: HealthState) -> str:
        """Stable ID from the source event: entity, rule, and first observation
        normalized to UTC, independent of when reconciliation ran."""
        first = health.evaluated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return f"incident:{health.entity_id}:{health.rule_version}:{first}"


def is_data_gap_incident(incident: Incident) -> bool:
    """Whether the incident's latest trigger was UNKNOWN health, i.e. a data gap.

    Such incidents were opened (or last updated) by earlier rule versions from
    expired or incomplete data, not from an observed deterioration.
    """
    return incident.trigger_metrics.get("health_status") == HealthStatus.UNKNOWN.value
