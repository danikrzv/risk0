from datetime import UTC, datetime
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
from tests.radar.repositories.builders import entity, snapshot


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
        def evaluate(self, entity, snapshot, dependencies, history):
            if entity.entity_id == MARKET:
                raise ValueError("bad metric")
            return super().evaluate(entity, snapshot, dependencies, history)

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
