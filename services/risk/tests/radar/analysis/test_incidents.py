from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.radar.analysis.incidents import IncidentDetector
from app.radar.contracts import HealthState, HealthStatus, IncidentSeverity, IncidentStatus


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def health(status: HealthStatus, at: datetime = NOW) -> HealthState:
    return HealthState(
        entity_id="oracle:1:test",
        status=status,
        score=None if status is HealthStatus.UNKNOWN else Decimal("0.5"),
        reasons=(f"{status.value.lower()} state",),
        missing_fields=(),
        evaluated_at=at,
        rule_version="oracle-health-v1",
    )


def test_opens_updates_escalates_and_resolves_one_incident_without_duplicates():
    detector = IncidentDetector()

    opened = detector.reconcile((), (health(HealthStatus.WARNING),), NOW)
    assert len(opened.opened) == 1
    incident = opened.current[0]
    assert incident.severity is IncidentSeverity.WARNING
    assert incident.status is IncidentStatus.OPEN

    stable = detector.reconcile(opened.current, (health(HealthStatus.WARNING),), NOW + timedelta(minutes=5))
    assert stable.opened == ()
    assert len(stable.updated) == 1
    assert stable.current[0].incident_id == incident.incident_id

    escalated = detector.reconcile(
        stable.current,
        (health(HealthStatus.CRITICAL, NOW + timedelta(minutes=10)),),
        NOW + timedelta(minutes=10),
    )
    assert escalated.current[0].severity is IncidentSeverity.CRITICAL
    assert escalated.current[0].first_observed_at == NOW

    resolved = detector.reconcile(
        escalated.current,
        (health(HealthStatus.HEALTHY, NOW + timedelta(minutes=15)),),
        NOW + timedelta(minutes=15),
    )
    assert len(resolved.resolved) == 1
    assert resolved.current[0].status is IncidentStatus.RESOLVED
    assert resolved.current[0].resolved_at == NOW + timedelta(minutes=15)


def test_delayed_health_observation_does_not_regress_incident():
    detector = IncidentDetector()
    opened = detector.reconcile((), (health(HealthStatus.CRITICAL),), NOW).current

    delayed = detector.reconcile(
        opened,
        (health(HealthStatus.WARNING, NOW - timedelta(minutes=5)),),
        NOW + timedelta(minutes=5),
    )

    assert delayed.opened == ()
    assert delayed.updated == ()
    assert delayed.current == opened


def test_unknown_health_opens_warning_incident_with_missing_data_reason():
    changes = IncidentDetector().reconcile((), (health(HealthStatus.UNKNOWN),), NOW)

    assert changes.current[0].severity is IncidentSeverity.WARNING
    assert "unknown state" in changes.current[0].reasons
