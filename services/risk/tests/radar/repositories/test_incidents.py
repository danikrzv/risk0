from datetime import timedelta
from decimal import Decimal

import pytest

from app.radar.contracts import (
    IncidentSeverity,
    IncidentStatus,
    Notification,
    RadarEntityType,
)
from tests.radar.repositories.builders import (
    CATALOG_VAULT,
    NEIGHBOR_VAULT,
    NOW,
    ORACLE,
    entity,
    impact,
    incident,
)


@pytest.fixture
def incidents(database):
    from app.radar.repositories import IncidentRepository, TopologyRepository

    TopologyRepository(database).upsert_entities(
        (
            entity(RadarEntityType.ORACLE, ORACLE.rsplit(":", 1)[1]),
            entity(),
            entity(key=NEIGHBOR_VAULT.rsplit(":", 1)[1]),
        ),
        seen_at=NOW,
    )
    return IncidentRepository(database)


def test_first_observation_opens_an_incident(incidents):
    result = incidents.upsert_active(incident())

    assert result.created is True
    assert result.incident == incident()
    assert incidents.get("incident-1") == incident()


def test_repeated_observation_updates_the_open_incident_without_duplicates(incidents):
    incidents.upsert_active(incident())
    later = NOW + timedelta(minutes=5)

    result = incidents.upsert_active(
        incident(
            "incident-generated-again",
            first_observed_at=later,
            metrics={"deviation": Decimal("0.11")},
        )
    )

    assert result.created is False
    assert result.incident.incident_id == "incident-1"
    assert result.incident.first_observed_at == NOW
    assert result.incident.last_observed_at == later
    assert result.incident.trigger_metrics == {"deviation": Decimal("0.11")}
    assert [i.incident_id for i in incidents.active()] == ["incident-1"]
    assert incidents.get("incident-generated-again") is None


def test_delayed_observation_does_not_regress_the_open_incident(incidents):
    later = NOW + timedelta(minutes=5)
    incidents.upsert_active(
        incident(last_observed_at=later, metrics={"deviation": Decimal("0.11")})
    )

    result = incidents.upsert_active(
        incident(
            "late",
            first_observed_at=NOW - timedelta(minutes=5),
            metrics={"deviation": Decimal("0.02")},
        )
    )

    assert result.created is False
    assert result.incident.first_observed_at == NOW - timedelta(minutes=5)
    assert result.incident.last_observed_at == later
    assert result.incident.trigger_metrics == {"deviation": Decimal("0.11")}


def test_open_incident_severity_only_escalates(incidents):
    incidents.upsert_active(incident(severity=IncidentSeverity.WARNING))
    escalated = incidents.upsert_active(
        incident(first_observed_at=NOW + timedelta(minutes=5))
    ).incident
    kept = incidents.upsert_active(
        incident(
            severity=IncidentSeverity.WARNING,
            first_observed_at=NOW + timedelta(minutes=10),
        )
    ).incident

    assert escalated.severity is IncidentSeverity.CRITICAL
    assert kept.severity is IncidentSeverity.CRITICAL
    assert kept.last_observed_at == NOW + timedelta(minutes=10)


def test_resolved_incident_is_kept_and_a_new_observation_opens_a_new_one(incidents):
    incidents.upsert_active(incident())
    resolved_at = NOW + timedelta(minutes=15)

    resolved = incidents.resolve("incident-1", resolved_at)
    reopened = incidents.upsert_active(
        incident("incident-2", first_observed_at=NOW + timedelta(hours=1))
    )

    assert resolved.status is IncidentStatus.RESOLVED
    assert resolved.resolved_at == resolved_at
    assert incidents.resolve("incident-1", resolved_at + timedelta(minutes=5)) == resolved
    assert incidents.resolve("missing", resolved_at) is None
    assert reopened.created is True
    assert [i.incident_id for i in incidents.active()] == ["incident-2"]
    assert [i.incident_id for i in incidents.list_incidents()] == [
        "incident-2",
        "incident-1",
    ]
    assert [
        i.incident_id for i in incidents.list_incidents(status=IncidentStatus.RESOLVED)
    ] == ["incident-1"]


def test_incidents_from_different_sources_or_rules_are_independent(incidents):
    incidents.upsert_active(incident())
    incidents.upsert_active(incident("incident-2", source_entity_id=CATALOG_VAULT))

    assert {i.incident_id for i in incidents.active()} == {"incident-1", "incident-2"}
    assert [i.incident_id for i in incidents.active(source_entity_id=ORACLE)] == [
        "incident-1"
    ]


def test_impacts_are_replaced_atomically(incidents):
    incidents.upsert_active(incident())
    incidents.replace_impacts(
        "incident-1", (impact(), impact(target_entity_id=NEIGHBOR_VAULT))
    )

    incidents.replace_impacts("incident-1", (impact(amount=None),))

    assert incidents.impacts("incident-1") == (impact(amount=None),)


def test_impacts_require_a_known_incident(incidents):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        incidents.replace_impacts("missing", (impact("missing"),))


def notification(notification_id: str, minutes: int = 0) -> Notification:
    return Notification(
        notification_id=notification_id,
        incident_id="incident-1",
        severity=IncidentSeverity.CRITICAL,
        title="Oracle deviation",
        message="Oracle deviates from consensus",
        created_at=NOW + timedelta(minutes=minutes),
    )


def test_notifications_are_idempotent_and_listed_newest_first(incidents):
    incidents.upsert_active(incident())

    assert incidents.add_notification(notification("n-1")) is True
    assert incidents.add_notification(notification("n-1")) is False
    incidents.add_notification(notification("n-2", minutes=5))
    incidents.add_notification(notification("n-3", minutes=10))

    first = incidents.list_notifications(limit=2)
    second = incidents.list_notifications(limit=2, cursor=first.next_cursor)

    assert [n.notification_id for n in first.notifications] == ["n-3", "n-2"]
    assert [n.notification_id for n in second.notifications] == ["n-1"]
    assert second.next_cursor is None


def test_notification_read_state_persists_and_keeps_the_first_read_time(incidents):
    incidents.upsert_active(incident())
    incidents.add_notification(notification("n-1"))
    incidents.add_notification(notification("n-2", minutes=5))
    read_at = NOW + timedelta(minutes=20)

    marked = incidents.mark_read("n-1", read_at)
    again = incidents.mark_read("n-1", read_at + timedelta(minutes=1))

    assert marked.read_at == read_at
    assert again.read_at == read_at
    assert incidents.mark_read("missing", read_at) is None
    assert [
        n.notification_id for n in incidents.list_notifications(unread_only=True).notifications
    ] == ["n-2"]
    assert incidents.get_notification("n-1").read_at == read_at


def test_reusing_a_resolved_incident_id_is_rejected(incidents):
    incidents.upsert_active(incident())
    incidents.resolve("incident-1", NOW + timedelta(minutes=5))

    with pytest.raises(ValueError):
        incidents.upsert_active(incident(first_observed_at=NOW + timedelta(hours=1)))

    assert incidents.get("incident-1").status is IncidentStatus.RESOLVED


def test_resolve_many_resolves_only_open_incidents_in_one_call(incidents):
    incidents.upsert_active(incident("incident-1"))
    incidents.upsert_active(incident("incident-2", source_entity_id=CATALOG_VAULT))
    resolved_at = NOW + timedelta(minutes=5)

    assert incidents.resolve_many(["incident-1", "incident-2", "missing"], resolved_at) == 2
    assert incidents.resolve_many(["incident-1"], resolved_at + timedelta(minutes=1)) == 0
    assert incidents.resolve_many([], resolved_at) == 0

    assert incidents.active() == ()
    assert incidents.get("incident-1").resolved_at == resolved_at


def test_mark_incidents_read_marks_only_unread_notifications_of_those_incidents(incidents):
    incidents.upsert_active(incident("incident-1"))
    incidents.upsert_active(incident("incident-2", source_entity_id=CATALOG_VAULT))
    incidents.add_notification(notification("n-1"))
    incidents.add_notification(
        notification("n-2", minutes=5).model_copy(update={"incident_id": "incident-2"})
    )
    read_at = NOW + timedelta(minutes=20)

    assert incidents.mark_incidents_read(["incident-1", "incident-1"], read_at) == 1
    assert incidents.mark_incidents_read(["incident-1"], read_at) == 0

    assert incidents.get_notification("n-1").read_at == read_at
    assert incidents.get_notification("n-2").read_at is None

def test_mark_all_read_marks_only_unread_notifications(incidents):
    incidents.upsert_active(incident())
    for index in range(3):
        incidents.add_notification(notification(f"n-{index}", minutes=index))
    incidents.mark_read("n-0", NOW + timedelta(minutes=10))

    assert incidents.mark_all_read(NOW + timedelta(minutes=20)) == 2
    assert incidents.mark_all_read(NOW + timedelta(minutes=30)) == 0
    assert incidents.get_notification("n-0").read_at == NOW + timedelta(minutes=10)
    assert incidents.get_notification("n-2").read_at == NOW + timedelta(minutes=20)


def test_top_active_ranks_severity_then_preferred_source_then_recency(incidents):
    incidents.upsert_active(
        incident("old-critical", severity=IncidentSeverity.CRITICAL)
    )
    incidents.upsert_active(
        incident(
            "new-warning",
            source_entity_id=NEIGHBOR_VAULT,
            severity=IncidentSeverity.WARNING,
            first_observed_at=NOW + timedelta(hours=2),
        )
    )
    incidents.upsert_active(
        incident(
            "preferred-warning",
            source_entity_id=CATALOG_VAULT,
            severity=IncidentSeverity.WARNING,
            first_observed_at=NOW + timedelta(hours=1),
        )
    )

    ranked = incidents.top_active(10, preferred_source_ids=(CATALOG_VAULT,))
    capped = incidents.top_active(1)
    restricted = incidents.top_active(10, source_ids=(NEIGHBOR_VAULT,))

    assert [item.incident_id for item in ranked] == [
        "old-critical",
        "preferred-warning",
        "new-warning",
    ]
    assert [item.incident_id for item in capped] == ["old-critical"]
    assert [item.incident_id for item in restricted] == ["new-warning"]


def test_path_entity_ids_keep_revealed_then_preferred_paths_per_incident(incidents):
    incidents.upsert_active(incident())
    hidden = "vault:1:0x3333333333333333333333333333333333333333"
    market = "market:1:0x" + "ab" * 32
    incidents.replace_impacts(
        "incident-1",
        (
            impact(target_entity_id=NEIGHBOR_VAULT),
            impact(target_entity_id=CATALOG_VAULT),
            impact(target_entity_id=hidden).model_copy(
                update={"path": (ORACLE, market, hidden), "distance": 2, "revealed": True}
            ),
        ),
    )

    everything = incidents.path_entity_ids(("incident-1",))
    top_two = incidents.path_entity_ids(
        ("incident-1",), paths_per_incident=2, preferred_target_ids=(CATALOG_VAULT,)
    )

    assert set(everything) == {ORACLE, market, hidden, CATALOG_VAULT, NEIGHBOR_VAULT}
    assert set(top_two) == {ORACLE, market, hidden, CATALOG_VAULT}
    assert incidents.path_entity_ids(()) == ()
