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


def test_unknown_does_not_downgrade_a_critical_incident():
    detector = IncidentDetector()
    opened = detector.reconcile((), (health(HealthStatus.CRITICAL),), NOW).current

    later = NOW + timedelta(minutes=5)
    changes = detector.reconcile(opened, (health(HealthStatus.UNKNOWN, later),), later)

    assert changes.current[0].severity is IncidentSeverity.CRITICAL
    assert changes.current[0].last_observed_at == later


def test_multiple_states_for_one_entity_in_a_batch_open_one_incident():
    later = NOW + timedelta(minutes=1)
    changes = IncidentDetector().reconcile(
        (),
        (
            health(HealthStatus.WARNING),
            health(HealthStatus.CRITICAL, later),
            health(HealthStatus.WARNING, NOW),
        ),
        later,
    )

    assert len(changes.opened) == 1
    assert len(changes.current) == 1
    assert changes.current[0].severity is IncidentSeverity.CRITICAL


def test_open_incident_for_unmonitored_entity_is_resolved():
    detector = IncidentDetector()
    opened = detector.reconcile((), (health(HealthStatus.CRITICAL),), NOW).current

    later = NOW + timedelta(minutes=5)
    kept = detector.reconcile(opened, (), later, monitored_entity_ids={"oracle:1:test"})
    removed = detector.reconcile(opened, (), later, monitored_entity_ids=set())

    assert kept.resolved == ()
    assert len(removed.resolved) == 1
    assert removed.current[0].status is IncidentStatus.RESOLVED
    assert removed.current[0].resolved_at == later


def test_incident_id_is_stable_and_independent_of_reconciliation_time():
    from datetime import timezone

    detector = IncidentDetector()
    paris = timezone(timedelta(hours=2))
    first = detector.reconcile((), (health(HealthStatus.WARNING),), NOW).current[0]
    second = detector.reconcile(
        (), (health(HealthStatus.WARNING),), (NOW + timedelta(minutes=7)).astimezone(paris)
    ).current[0]

    assert first.incident_id == second.incident_id
    assert first.incident_id == "incident:oracle:1:test:oracle-health-v1:2026-10-10T12:00:00Z"
