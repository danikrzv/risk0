from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.radar.contracts import (
    ExposureCreateRequest,
    HealthStatus,
    RadarEntityType,
    RadarRelationship,
)
from app.radar.discovery.morpho import DiscoveryBatch
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)
from app.radar.service import RadarService
from tests.radar.repositories.builders import entity, health, snapshot


NOW = datetime(2026, 10, 10, 13, 11, tzinfo=UTC)
MARKET = "market:1:market-a"
VAULT = "vault:1:vault-a"


@pytest.fixture
def database(tmp_path):
    database = RadarDatabase(tmp_path / "radar.sqlite3")
    database.migrate()
    return database


class FixedDiscovery:
    def __init__(self, batch: DiscoveryBatch) -> None:
        self.batch = batch

    def discover(self, now: datetime) -> DiscoveryBatch:
        assert now == NOW
        return self.batch


class FixedMorphoCollector:
    def __init__(self, snapshots: tuple) -> None:
        self.snapshots = snapshots

    def collect(self, batch: DiscoveryBatch, now: datetime) -> tuple:
        assert batch.discovered_at == NOW
        assert now == NOW
        return self.snapshots


class EmptyChainCollector:
    def collect_all(self, chain_ids: tuple[int, ...], now: datetime) -> tuple:
        assert chain_ids == (1,)
        assert now == NOW
        return ()


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, object]]] = []

    def publish(self, event_type: str, resource_id: str, summary: dict[str, object]):
        self.items.append((event_type, resource_id, summary))


def vertical_batch() -> DiscoveryBatch:
    market = entity(RadarEntityType.MARKET, "market-a")
    vault = entity(RadarEntityType.VAULT, "vault-a", catalog=True)
    relationship = RadarRelationship(
        source_entity_id=VAULT,
        target_entity_id=MARKET,
        relationship_type="allocated_to",
        weight=Decimal("1"),
    )
    return DiscoveryBatch(
        discovered_at=NOW,
        chains=(),
        protocols=(),
        vaults=(vault,),
        markets=(market,),
        assets=(),
        oracles=(),
        irms=(),
        relationships=(relationship,),
        sources=(),
        failures=(),
        complete_chain_ids=(1,),
        market_records=(),
        vault_records=(),
    )


CRITICAL_MARKET_METRICS = {
    "utilization": Decimal("0.50"),
    "available_liquidity": Decimal("100"),
    "lender_hhi": Decimal("0.10"),
    "borrower_hhi": Decimal("0.10"),
    "near_liquidation_debt_ratio": Decimal("0.05"),
    "bad_debt": Decimal("1"),
}


def build_service(database, collector, **overrides) -> RadarService:
    values = {
        "discovery": FixedDiscovery(vertical_batch()),
        "morpho_collector": collector,
        "chain_collector": EmptyChainCollector(),
        "topology": TopologyRepository(database),
        "states": StateRepository(database),
        "incidents": IncidentRepository(database),
        "exposures": ExposureRepository(database),
    }
    values.update(overrides)
    return RadarService(**values)


class CountingPropagation:
    def __init__(self) -> None:
        from app.radar.analysis import PropagationEngine

        self.engine = PropagationEngine()
        self.calls = 0

    def propagate(self, *args, **kwargs):
        self.calls += 1
        return self.engine.propagate(*args, **kwargs)


class SequencedCollector:
    """Returns one new market snapshot per call at the given minutes."""

    def __init__(self, observed: list[datetime]) -> None:
        self.observed = observed

    def collect(self, batch: DiscoveryBatch, now: datetime) -> tuple:
        return tuple(
            snapshot(MARKET, at, metrics=CRITICAL_MARKET_METRICS)
            for at in self.observed
            if at <= now
        )


def test_unchanged_cycles_do_not_repropagate_until_topology_changes(database):
    later = datetime(2026, 10, 10, 13, 16, tzinfo=UTC)
    propagation = CountingPropagation()
    service = build_service(
        database, SequencedCollector([NOW]), propagation=propagation
    )
    service._discovery = type(
        "AnyTimeDiscovery", (), {"discover": lambda self, now: vertical_batch()}
    )()

    service.run_discovery(NOW)
    service.run_monitoring(NOW)
    assert propagation.calls == 1

    service.run_monitoring(later)
    service.run_monitoring(later)
    assert propagation.calls == 1

    service.run_discovery(later)
    service.run_monitoring(later)
    assert propagation.calls == 2


def test_one_failing_entity_evaluation_does_not_block_the_others(database):
    from app.radar.analysis import HealthEvaluator

    class FailingForMarket(HealthEvaluator):
        def evaluate(self, entity, snapshot, dependencies, history, **kwargs):
            if entity.entity_id == MARKET:
                raise ValueError("bad metric")
            return super().evaluate(entity, snapshot, dependencies, history, **kwargs)

    class TwoEntityCollector:
        def collect(self, batch, now):
            return (
                snapshot(MARKET, NOW, metrics=CRITICAL_MARKET_METRICS),
                snapshot(VAULT, NOW, metrics={"utilization": Decimal("0.5")}),
            )

    states = StateRepository(database)
    service = build_service(
        database, TwoEntityCollector(), health=FailingForMarket(), states=states
    )
    service.run_discovery(NOW)
    run = service.run_monitoring(NOW)

    assert run.status == "partial"
    assert run.error_count == 1
    assert states.latest_health(MARKET) is None
    assert states.latest_health(VAULT) is not None


def test_vertical_monitoring_cycle_is_persisted_and_idempotent(database):
    market = entity(RadarEntityType.MARKET, "market-a")
    vault = entity(RadarEntityType.VAULT, "vault-a", catalog=True)
    relationship = RadarRelationship(
        source_entity_id=VAULT,
        target_entity_id=MARKET,
        relationship_type="allocated_to",
        weight=Decimal("1"),
    )
    batch = DiscoveryBatch(
        discovered_at=NOW,
        chains=(),
        protocols=(),
        vaults=(vault,),
        markets=(market,),
        assets=(),
        oracles=(),
        irms=(),
        relationships=(relationship,),
        sources=(),
        failures=(),
        complete_chain_ids=(1,),
        market_records=(),
        vault_records=(),
    )
    market_snapshot = snapshot(
        MARKET,
        NOW,
        metrics={
            "utilization": Decimal("0.50"),
            "available_liquidity": Decimal("100"),
            "lender_hhi": Decimal("0.10"),
            "borrower_hhi": Decimal("0.10"),
            "near_liquidation_debt_ratio": Decimal("0.05"),
            "bad_debt": Decimal("1"),
        },
    )
    topology = TopologyRepository(database)
    states = StateRepository(database)
    incidents = IncidentRepository(database)
    exposures = ExposureRepository(database)
    events = RecordingEvents()
    service = RadarService(
        discovery=FixedDiscovery(batch),
        morpho_collector=FixedMorphoCollector((market_snapshot,)),
        chain_collector=EmptyChainCollector(),
        topology=topology,
        states=states,
        incidents=incidents,
        exposures=exposures,
        events=events,
    )

    discovery_run = service.run_discovery(NOW)
    exposure = exposures.create(
        ExposureCreateRequest(
            entity_id=VAULT,
            mode="lender",
            amount=Decimal("1000"),
            numeraire="USDC",
        ),
        created_at=NOW,
        exposure_id="exposure-1",
    )
    first = service.run_monitoring(NOW)
    second = service.run_monitoring(NOW)

    assert discovery_run.status == "completed"
    assert first.status == "completed"
    assert first.processed_count == 1
    assert second.status == "completed"
    assert second.processed_count == 0
    assert len(states.history(MARKET).snapshots) == 1
    assert states.latest_health(MARKET).status is HealthStatus.CRITICAL
    assert len(incidents.active()) == 1
    incident = incidents.active()[0]
    assert [impact.target_entity_id for impact in incidents.impacts(incident.incident_id)] == [VAULT]
    assert exposures.impacts(exposure.exposure_id)[0].estimated_affected_amount == Decimal("1000")
    assert len(incidents.list_notifications().notifications) == 1
    assert [item[0] for item in events.items] == [
        "graph.updated",
        "incident.opened",
        "notification.created",
        "graph.updated",
    ]


def test_monitoring_without_topology_records_a_partial_run(database):
    service = RadarService(
        discovery=None,
        morpho_collector=None,
        chain_collector=None,
        topology=TopologyRepository(database),
        states=StateRepository(database),
        incidents=IncidentRepository(database),
        exposures=ExposureRepository(database),
    )

    run = service.run_monitoring(NOW)

    assert run.status == "partial"
    assert run.error_count == 1
    assert "discovery" in run.message.lower()


def test_vault_health_receives_allocation_weights_from_topology(database):
    from app.radar.analysis import HealthEvaluator

    seen: dict[str, object] = {}

    class RecordingHealth(HealthEvaluator):
        def evaluate(self, entity, snapshot, dependencies, history, **kwargs):
            seen[entity.entity_id] = kwargs.get("allocations")
            return super().evaluate(entity, snapshot, dependencies, history, **kwargs)

    class TwoEntityCollector:
        def collect(self, batch, now):
            return (
                snapshot(MARKET, NOW, metrics=CRITICAL_MARKET_METRICS),
                snapshot(VAULT, NOW, metrics={"utilization": Decimal("0.5")}),
            )

    service = build_service(database, TwoEntityCollector(), health=RecordingHealth())
    service.run_discovery(NOW)
    service.run_monitoring(NOW)

    assert seen[VAULT] == {MARKET: Decimal("1")}
    assert seen[MARKET] is None


def test_incident_reconcile_receives_the_monitored_entity_ids(database):
    from app.radar.analysis import IncidentDetector

    seen: list[object] = []

    class RecordingDetector(IncidentDetector):
        def reconcile(self, previous, health_states, now, **kwargs):
            seen.append(kwargs.get("monitored_entity_ids"))
            return super().reconcile(previous, health_states, now, **kwargs)

    service = build_service(
        database,
        SequencedCollector([NOW]),
        incident_detector=RecordingDetector(),
    )
    service.run_discovery(NOW)
    service.run_monitoring(NOW)

    assert seen and set(seen[-1]) == {MARKET, VAULT}


FIVE_MINUTES = timedelta(minutes=5)
WARNING_MARKET_METRICS = {**CRITICAL_MARKET_METRICS, "bad_debt": Decimal("0"), "utilization": Decimal("0.95")}
BASE_MARKET = "market:8453:market-b"


class AnyTimeChainCollector:
    def __init__(self) -> None:
        self.chain_ids: list[tuple[int, ...]] = []

    def collect_all(self, chain_ids, now):
        self.chain_ids.append(tuple(chain_ids))
        return ()


class PerCycleCollector:
    """Builds each cycle's snapshots from the cycle time."""

    def __init__(self, build) -> None:
        self.build = build

    def collect(self, batch, now):
        return tuple(self.build(now))


def markets_batch(*markets) -> DiscoveryBatch:
    return DiscoveryBatch(
        discovered_at=NOW,
        chains=(),
        protocols=(),
        vaults=(),
        markets=tuple(markets),
        assets=(),
        oracles=(),
        irms=(),
        relationships=(),
        sources=(),
        failures=(),
        complete_chain_ids=tuple(sorted({market.chain_id for market in markets})),
        market_records=(),
        vault_records=(),
    )


def gap_incident(entity_id: str = MARKET):
    from app.radar.contracts import Incident

    return Incident(
        incident_id=f"incident:{entity_id}:radar-health-v1:gap",
        source_entity_id=entity_id,
        severity="WARNING",
        status="OPEN",
        trigger_metrics={"health_status": "UNKNOWN", "health_score": None},
        rule_version="radar-health-v1",
        first_observed_at=NOW - FIVE_MINUTES,
        last_observed_at=NOW - FIVE_MINUTES,
        reasons=("snapshot is expired",),
    )


def test_expired_or_incomplete_data_is_unknown_without_incident_or_notification(database):
    def gaps(now):
        return (
            snapshot(MARKET, now, metrics={}, freshness="expired", missing_fields=("state",)),
            snapshot(
                VAULT,
                now,
                metrics={"withdrawal_liquidity": None},
                missing_fields=("withdrawal_liquidity",),
            ),
        )

    states = StateRepository(database)
    incidents = IncidentRepository(database)
    events = RecordingEvents()
    service = build_service(
        database,
        PerCycleCollector(gaps),
        chain_collector=AnyTimeChainCollector(),
        states=states,
        incidents=incidents,
        events=events,
    )
    service.run_discovery(NOW)
    service.run_monitoring(NOW)
    service.run_monitoring(NOW + FIVE_MINUTES)

    assert states.latest_health(MARKET).status is HealthStatus.UNKNOWN
    assert states.latest_health(MARKET).reasons == ("snapshot is expired",)
    assert states.latest_health(VAULT).status is HealthStatus.UNKNOWN
    assert incidents.list_incidents() == ()
    assert incidents.list_notifications().notifications == ()
    assert not [item for item in events.items if item[0] != "graph.updated"]


def test_open_data_gap_incidents_are_resolved_and_their_alerts_read_next_cycle(database):
    from app.radar.contracts import Notification

    topology = TopologyRepository(database)
    states = StateRepository(database)
    incidents = IncidentRepository(database)
    topology.upsert_entities(vertical_batch().entities, seen_at=NOW)
    gap = incidents.upsert_active(gap_incident()).incident
    states.set_health(
        health(
            entity_id=gap.source_entity_id,
            evaluated_at=gap.first_observed_at,
            status=HealthStatus.UNKNOWN,
        )
    )
    incidents.add_notification(
        Notification(
            notification_id=f"notification:{gap.incident_id}",
            incident_id=gap.incident_id,
            severity="WARNING",
            title="WARNING: radar incident",
            message="snapshot is expired",
            created_at=gap.first_observed_at,
        )
    )
    events = RecordingEvents()
    service = build_service(
        database,
        PerCycleCollector(lambda now: ()),
        chain_collector=AnyTimeChainCollector(),
        states=states,
        incidents=incidents,
        events=events,
    )

    service.run_discovery(NOW)
    service.run_monitoring(NOW)

    assert incidents.active() == ()
    assert incidents.get(gap.incident_id).resolved_at == NOW
    assert incidents.list_notifications(unread_only=True).notifications == ()
    assert ("incident.resolved", gap.incident_id) in [item[:2] for item in events.items]


def test_only_allowlisted_chains_are_monitored_and_raise_incidents(database):
    from app.radar.contracts import Incident

    base_market = entity(RadarEntityType.MARKET, "market-b", chain_id=8453)
    batch = markets_batch(entity(RadarEntityType.MARKET, "market-a"), base_market)
    topology = TopologyRepository(database)
    states = StateRepository(database)
    incidents = IncidentRepository(database)
    topology.upsert_entities(batch.entities, seen_at=NOW)
    base_incident = incidents.upsert_active(
        Incident(
            incident_id="incident:base",
            source_entity_id=BASE_MARKET,
            severity="CRITICAL",
            status="OPEN",
            trigger_metrics={"health_status": "CRITICAL", "health_score": "0"},
            rule_version="radar-health-v1",
            first_observed_at=NOW - FIVE_MINUTES,
            last_observed_at=NOW - FIVE_MINUTES,
            reasons=("bad debt detected",),
        )
    ).incident
    chains = AnyTimeChainCollector()
    service = build_service(
        database,
        PerCycleCollector(
            lambda now: (
                snapshot(MARKET, now, metrics=CRITICAL_MARKET_METRICS),
                snapshot(BASE_MARKET, now, metrics=CRITICAL_MARKET_METRICS),
            )
        ),
        discovery=FixedDiscovery(batch),
        chain_collector=chains,
        topology=topology,
        states=states,
        incidents=incidents,
        chain_ids=(1,),
    )

    service.run_discovery(NOW)
    service.run_monitoring(NOW)

    assert chains.chain_ids == [(1,)]
    assert states.history(BASE_MARKET).snapshots == ()
    assert states.latest_health(BASE_MARKET) is None
    assert [incident.source_entity_id for incident in incidents.active()] == [MARKET]
    assert incidents.get(base_incident.incident_id).status == "RESOLVED"


def test_unchanged_observations_confirm_hysteresis_without_rewriting_history(database):
    states = StateRepository(database)
    incidents = IncidentRepository(database)
    events = RecordingEvents()
    service = build_service(
        database,
        PerCycleCollector(lambda now: (snapshot(MARKET, now, metrics=WARNING_MARKET_METRICS),)),
        discovery=FixedDiscovery(markets_batch(entity(RadarEntityType.MARKET, "market-a"))),
        chain_collector=AnyTimeChainCollector(),
        states=states,
        incidents=incidents,
        events=events,
    )
    service.run_discovery(NOW)
    events.items.clear()

    runs = [service.run_monitoring(NOW + FIVE_MINUTES * cycle) for cycle in range(4)]

    # Every cycle is a new observation, but the state never changed: one snapshot.
    assert [run.processed_count for run in runs] == [1, 1, 1, 1]
    assert len(states.history(MARKET).snapshots) == 1
    # Pending warning, confirmed warning; then unchanged health is not rewritten.
    assert [h.status for h in states.history(MARKET).health_states] == [
        HealthStatus.WARNING,
        HealthStatus.HEALTHY,
    ]
    assert len(incidents.list_incidents()) == 1
    assert len(incidents.list_notifications().notifications) == 1
    assert incidents.active()[0].last_observed_at == NOW + FIVE_MINUTES * 3
    assert [item[0] for item in events.items] == [
        "graph.updated",
        "incident.opened",
        "notification.created",
        "graph.updated",
    ]


def test_changing_metric_values_alone_do_not_rewrite_health(database):
    """The derivation changes every cycle; health is still written only on change."""
    states = StateRepository(database)
    utilization = [Decimal("0.95"), Decimal("0.96"), Decimal("0.955"), Decimal("0.951")]
    service = build_service(
        database,
        PerCycleCollector(
            lambda now: (
                snapshot(
                    MARKET,
                    now,
                    metrics={
                        **WARNING_MARKET_METRICS,
                        "utilization": utilization[(now - NOW) // FIVE_MINUTES],
                    },
                ),
            )
        ),
        discovery=FixedDiscovery(markets_batch(entity(RadarEntityType.MARKET, "market-a"))),
        chain_collector=AnyTimeChainCollector(),
        states=states,
        events=RecordingEvents(),
    )
    service.run_discovery(NOW)

    for cycle in range(4):
        service.run_monitoring(NOW + FIVE_MINUTES * cycle)

    # Pending warning, then the confirmed warning; later cycles only changed values.
    assert [h.status for h in states.history(MARKET).health_states] == [
        HealthStatus.WARNING,
        HealthStatus.HEALTHY,
    ]
    latest = states.latest_health(MARKET)
    assert latest.evaluated_at == NOW + FIVE_MINUTES
    # The stored derivation explains the stored evaluation, not the last cycle.
    utilization_check = next(
        check
        for check in latest.derivation.checks
        if check.metric == "utilization" and check.leads_to is HealthStatus.WARNING
    )
    assert utilization_check.observed == Decimal("0.96")
    assert latest.derivation.final_status is HealthStatus.WARNING


def test_one_cycle_emits_one_graph_update_and_a_bounded_number_of_incident_events(database):
    from app.radar.service import MAX_INCIDENT_EVENTS, MAX_NOTIFICATION_EVENTS

    count = MAX_INCIDENT_EVENTS + 10
    markets = [entity(RadarEntityType.MARKET, f"market-{index:02d}") for index in range(count)]
    incidents = IncidentRepository(database)
    events = RecordingEvents()
    service = build_service(
        database,
        PerCycleCollector(
            lambda now: [
                snapshot(market.entity_id, now, metrics=CRITICAL_MARKET_METRICS)
                for market in markets
            ]
        ),
        discovery=FixedDiscovery(markets_batch(*markets)),
        chain_collector=AnyTimeChainCollector(),
        incidents=incidents,
        events=events,
    )
    service.run_discovery(NOW)
    events.items.clear()

    service.run_monitoring(NOW)

    types = [item[0] for item in events.items]
    assert len(incidents.active()) == count
    assert len(incidents.list_notifications(limit=100).notifications) == count
    assert types.count("graph.updated") == 1
    # The budget includes the one summary event standing for the remainder.
    assert len([kind for kind in types if kind.startswith("incident.")]) == MAX_INCIDENT_EVENTS
    assert types.count("incident.opened") == MAX_INCIDENT_EVENTS - 1
    assert types.count("notification.created") == MAX_NOTIFICATION_EVENTS
    omitted = count - MAX_INCIDENT_EVENTS + 1
    incident_summary = next(item for item in events.items if item[1] == "incidents")
    assert incident_summary == (
        "incident.updated",
        "incidents",
        {"coalesced": True, "count": omitted, "opened": omitted},
    )
    notification_summary = next(item for item in events.items if item[1] == "notifications")
    assert notification_summary[0] == "notification.created"
    assert notification_summary[2]["count"] == count - MAX_NOTIFICATION_EVENTS + 1


def test_slow_cycle_aggregates_completed_hours_and_applies_retention(database):
    topology = TopologyRepository(database)
    states = StateRepository(database)
    topology.upsert_entities(vertical_batch().entities, seen_at=NOW - timedelta(days=40))
    old = NOW - timedelta(days=31)
    recent = NOW - timedelta(hours=2)
    states.append_snapshot(snapshot(MARKET, old))
    states.append_snapshot(snapshot(MARKET, recent))
    service = build_service(
        database, FixedMorphoCollector(()), topology=topology, states=states
    )

    run = service.run_discovery(NOW)

    assert run.status == "completed"
    assert [item.observed_at for item in states.history(MARKET).snapshots] == [recent]
    hour = recent.replace(minute=0)
    assert states.hourly(MARKET, start=hour, end=hour + timedelta(hours=1))
    old_hour = old.replace(minute=0)
    assert states.hourly(MARKET, start=old_hour, end=old_hour + timedelta(hours=1))


def test_retention_failure_marks_the_slow_cycle_partial(database):
    class FailingRetention(StateRepository):
        def prune(self, now):
            raise RuntimeError("database is locked")

    service = build_service(
        database, FixedMorphoCollector(()), states=FailingRetention(database)
    )

    run = service.run_discovery(NOW)

    assert run.status == "partial"
    assert run.error_count == 1
    assert "Retention failed: RuntimeError" in run.message
