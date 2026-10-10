import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.api.events import RadarEventBroker, get_event_broker
from app.main import app
from app.radar.contracts import (
    HealthStatus,
    IncidentImpact,
    IncidentSeverity,
    Notification,
    RadarEntityType,
    RadarRelationship,
    SimilarityRecord,
)
from app.radar.repositories import (
    ExposureRepository,
    IncidentRepository,
    RadarDatabase,
    StateRepository,
    TopologyRepository,
)
from app.radar.synthetic import build_synthetic_radar
from tests.radar.repositories.builders import NOW, entity, health, incident


SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "packages/contracts/schemas/radar.schema.json"
)
CATALOG_VAULT = "vault:1:catalog"
MARKET = "market:1:market-a"
ORACLE = "oracle:1:oracle-a"
HUB_ASSET = "asset:1:hub"
SIDE_MARKET = "market:1:side"
HIDDEN_VAULT = "vault:1:hidden"
REVEALED_VAULT = "vault:1:revealed"
FAR_MARKET = "market:1:far"
FAR_VAULT = "vault:1:far"
HUB_MARKETS = tuple(f"market:1:hub-{index:03d}" for index in range(70))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def broker():
    return RadarEventBroker()


@pytest.fixture
def context(tmp_path, clock):
    from app.api.radar import GraphCache, RadarContext

    database = RadarDatabase(tmp_path / "graph.sqlite3")
    database.migrate()
    return RadarContext(
        topology=TopologyRepository(database),
        states=StateRepository(database),
        incidents=IncidentRepository(database),
        exposures=ExposureRepository(database),
        database=database,
        graph_cache=GraphCache(clock=clock),
    )


@pytest.fixture
def client(context, broker):
    from app.api.radar import get_radar_context

    app.dependency_overrides[get_radar_context] = lambda: context
    app.dependency_overrides[get_event_broker] = lambda: broker
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _entity(entity_id: str, **overrides):
    entity_type, chain_id, key = entity_id.split(":", 2)
    return entity(
        RadarEntityType(entity_type), key, int(chain_id), **{"visible": False, **overrides}
    )


def _relationship(source: str, target: str, relationship_type: str, weight=None):
    return RadarRelationship(
        source_entity_id=source,
        target_entity_id=target,
        relationship_type=relationship_type,
        weight=weight,
    )


def _similarity(candidate: str, catalog_vault: str = CATALOG_VAULT) -> SimilarityRecord:
    return SimilarityRecord(
        catalog_entity_id=catalog_vault,
        candidate_entity_id=candidate,
        score=Decimal("0.5"),
        components={"shared_markets": Decimal("0.5")},
        stored=True,
        normally_visible=False,
        computed_at=NOW,
    )


def seed_radar(context) -> None:
    """A catalog vault's neighbourhood with each pruning rule exercised once."""
    context.topology.upsert_entities(
        (
            _entity(CATALOG_VAULT, catalog=True, visible=True),
            _entity(MARKET, visible=True),
            _entity(ORACLE, visible=True),
            _entity(HUB_ASSET, visible=True),
            _entity(SIDE_MARKET),
            _entity(HIDDEN_VAULT),
            _entity(REVEALED_VAULT),
            _entity(FAR_MARKET),
            _entity(FAR_VAULT),
            *(_entity(market) for market in HUB_MARKETS),
        ),
        seen_at=NOW,
    )
    context.topology.upsert_relationships(
        (
            _relationship(CATALOG_VAULT, MARKET, "allocated_to", Decimal("1")),
            _relationship(MARKET, ORACLE, "uses_oracle"),
            _relationship(MARKET, HUB_ASSET, "lends"),
            # A lateral neighbour of the (non-hub) oracle.
            _relationship(SIDE_MARKET, ORACLE, "uses_oracle"),
            # Hidden vaults are never plain neighbours.
            _relationship(HIDDEN_VAULT, MARKET, "allocated_to", Decimal("0.5")),
            _relationship(REVEALED_VAULT, MARKET, "allocated_to", Decimal("0.4")),
            # The hub asset's other markets are only listed on request.
            *(_relationship(market, HUB_ASSET, "lends") for market in HUB_MARKETS),
            _relationship(FAR_VAULT, FAR_MARKET, "allocated_to", Decimal("1")),
        ),
        seen_at=NOW,
    )
    context.topology.replace_similarities(
        CATALOG_VAULT, (_similarity(REVEALED_VAULT), _similarity(FAR_VAULT))
    )
    oracle_incident = incident("incident-oracle", source_entity_id=ORACLE)
    context.incidents.upsert_active(oracle_incident)
    context.incidents.replace_impacts(
        oracle_incident.incident_id,
        (
            IncidentImpact(
                incident_id=oracle_incident.incident_id,
                target_entity_id=REVEALED_VAULT,
                path=(ORACLE, MARKET, REVEALED_VAULT),
                distance=2,
                confidence=Decimal("0.8"),
                estimated_affected_amount=None,
                reasons=("allocates to the affected market",),
                revealed=True,
            ),
        ),
    )


def ids(items) -> set[str]:
    return {item["entity_id"] for item in items}


def test_graph_returns_focus_set_neighbours_and_incident_paths_only(client, context):
    seed_radar(context)

    body = client.get("/v1/graph").json()

    assert ids(body["entities"]) == {
        CATALOG_VAULT,
        MARKET,
        ORACLE,
        HUB_ASSET,
        SIDE_MARKET,
        REVEALED_VAULT,
    }
    returned = ids(body["entities"])
    assert body["relationships"]
    assert all(
        item["source_entity_id"] in returned and item["target_entity_id"] in returned
        for item in body["relationships"]
    )
    assert [item["candidate_entity_id"] for item in body["similarities"]] == [
        REVEALED_VAULT
    ]
    assert [item["incident_id"] for item in body["active_incidents"]] == [
        "incident-oracle"
    ]


def test_focus_expands_one_entity_even_when_it_is_a_hub(client, context):
    seed_radar(context)

    body = client.get("/v1/graph", params={"focus": HUB_ASSET}).json()
    missing = client.get("/v1/graph", params={"focus": "asset:1:missing"})

    assert set(HUB_MARKETS) <= ids(body["entities"])
    assert HIDDEN_VAULT not in ids(body["entities"])
    assert missing.status_code == 404


def test_filters_narrow_the_pruned_view(client, context):
    seed_radar(context)
    context.states.set_health(health(MARKET, NOW, HealthStatus.CRITICAL))

    vaults = client.get("/v1/graph", params={"entity_type": "vault"}).json()
    critical = client.get("/v1/graph", params={"status": "CRITICAL"}).json()
    footprint = client.get("/v1/graph", params={"incident_id": "incident-oracle"}).json()

    assert ids(vaults["entities"]) == {CATALOG_VAULT, REVEALED_VAULT}
    assert vaults["active_incidents"] == []
    assert ids(critical["entities"]) == {MARKET}
    assert critical["entities"][0]["metadata"]["health_status"] == "CRITICAL"
    assert ids(footprint["entities"]) == {ORACLE, MARKET, REVEALED_VAULT}
    assert len(footprint["active_incidents"]) == 1


def test_graph_returns_only_entities_on_monitored_chains(client, context):
    seed_radar(context)
    base_vault = "vault:8453:0xbase"
    context.topology.upsert_entities(
        (_entity(base_vault, catalog=True, visible=True),), seen_at=NOW
    )

    unrestricted = client.get("/v1/graph").json()
    object.__setattr__(context, "chain_ids", (1,))
    context.graph_cache.invalidate()
    restricted = client.get("/v1/graph").json()

    assert base_vault in ids(unrestricted["entities"])
    assert base_vault not in ids(restricted["entities"])
    assert CATALOG_VAULT in ids(restricted["entities"])


def test_active_incidents_are_capped_most_severe_then_focus_then_newest(
    client, context
):
    from app.api.radar import GRAPH_INCIDENT_LIMIT

    seed_radar(context)
    sources = [f"market:1:incident-{index:03d}" for index in range(GRAPH_INCIDENT_LIMIT + 10)]
    context.topology.upsert_entities(
        tuple(_entity(source) for source in sources), seen_at=NOW
    )
    for index, source in enumerate(sources):
        context.incidents.upsert_active(
            incident(
                f"incident-{index:03d}",
                source_entity_id=source,
                severity=IncidentSeverity.CRITICAL if index < 3 else IncidentSeverity.WARNING,
                first_observed_at=NOW + timedelta(minutes=index),
            )
        )
    # Oldest of all, but sourced on the focus set.
    context.incidents.upsert_active(
        incident(
            "incident-focus",
            source_entity_id=MARKET,
            severity=IncidentSeverity.WARNING,
            first_observed_at=NOW - timedelta(days=1),
        )
    )

    listed = [
        item["incident_id"] for item in client.get("/v1/graph").json()["active_incidents"]
    ]

    assert len(listed) == GRAPH_INCIDENT_LIMIT
    assert listed[:5] == [
        # CRITICAL, sourced on the focus set (the oracle) first.
        "incident-oracle",
        "incident-002",
        "incident-001",
        "incident-000",
        # Oldest of all, but a WARNING sourced on the focus set.
        "incident-focus",
    ]
    assert listed[5] == f"incident-{GRAPH_INCIDENT_LIMIT + 9:03d}"

def test_graph_cache_serves_within_ttl_and_rebuilds_on_events_and_user_writes(
    client, context, clock, broker, monkeypatch
):
    from app.api.radar import GRAPH_CACHE_TTL_SECONDS

    seed_radar(context)
    context.incidents.add_notification(
        Notification(
            notification_id="notification-1",
            incident_id="incident-oracle",
            severity=IncidentSeverity.CRITICAL,
            title="Oracle critical",
            message="Oracle health crossed the critical threshold.",
            created_at=NOW,
        )
    )
    builds = {"count": 0}
    focus_ids = context.topology.catalog_or_visible_ids

    def counted():
        builds["count"] += 1
        return focus_ids()

    monkeypatch.setattr(context.topology, "catalog_or_visible_ids", counted)

    def market_health() -> str | None:
        body = client.get("/v1/graph").json()
        market = next(item for item in body["entities"] if item["entity_id"] == MARKET)
        return market["metadata"].get("health_status")

    assert market_health() is None
    assert market_health() is None
    assert builds["count"] == 1

    # Background writes are served stale for the TTL...
    context.states.set_health(health(MARKET, NOW, HealthStatus.WARNING))
    clock.now += GRAPH_CACHE_TTL_SECONDS / 2
    assert market_health() is None
    assert builds["count"] == 1
    # ...and picked up once it expires.
    clock.now += GRAPH_CACHE_TTL_SECONDS
    assert market_health() == "WARNING"
    assert builds["count"] == 2

    # An SSE event announces a write: the refetch it triggers must see it.
    context.states.set_health(health(MARKET, NOW + timedelta(minutes=1), HealthStatus.CRITICAL))
    broker.publish("graph.updated", "graph", {})
    assert market_health() == "CRITICAL"
    assert builds["count"] == 3

    # User-initiated writes invalidate immediately, even at the same revision.
    for request in (
        lambda: client.post("/v1/notifications/notification-1/read"),
        lambda: client.post("/v1/notifications/read-all"),
        lambda: client.post(
            "/v1/exposures",
            json={"entity_id": CATALOG_VAULT, "mode": "lender", "amount": "10", "numeraire": "USDC"},
        ),
    ):
        before = builds["count"]
        assert request().status_code in (200, 201)
        client.get("/v1/graph")
        assert builds["count"] == before + 1


def test_graph_cache_drops_a_build_overtaken_by_an_invalidation(clock):
    from app.api.radar import GraphCache, RadarGraphResponse

    cache = GraphCache(clock=clock)
    response = RadarGraphResponse(
        entities=(), relationships=(), similarities=(), active_incidents=(), generated_at=NOW
    )
    generation = cache.generation
    cache.invalidate()
    cache.put(generation, 1, 0, ("key",), response)
    assert cache.get(1, 0, ("key",)) is None

    cache.put(cache.generation, 2, 0, ("key",), response)
    # An older revision never replaces a newer entry.
    cache.put(cache.generation, 1, 0, ("key",), response.model_copy())
    assert cache.get(2, 0, ("key",)) is response


def test_mark_all_notifications_read_counts_only_unread(client, context):
    seed_radar(context)
    for index in range(3):
        context.incidents.add_notification(
            Notification(
                notification_id=f"notification-{index}",
                incident_id="incident-oracle",
                severity=IncidentSeverity.CRITICAL,
                title="Oracle critical",
                message="Oracle health crossed the critical threshold.",
                created_at=NOW + timedelta(minutes=index),
            )
        )
    client.post("/v1/notifications/notification-0/read")

    first = client.post("/v1/notifications/read-all")
    second = client.post("/v1/notifications/read-all")
    unread = client.get("/v1/notifications", params={"unread_only": True}).json()

    assert first.status_code == 200
    assert first.json() == {"schema_version": "1.0.0", "updated": 2}
    assert second.json() == {"schema_version": "1.0.0", "updated": 0}
    assert unread["notifications"] == []
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator(
        {"$ref": "#/$defs/NotificationReadAllResponse", "$defs": schema["$defs"]}
    ).validate(first.json())


# Upper bound for the default view of a live-sized radar; the unpruned graph
# of the same data is tens of megabytes.
GRAPH_PAYLOAD_BUDGET_BYTES = 500_000


def test_graph_payload_stays_small_on_a_live_sized_radar(client, context):
    seeded = build_synthetic_radar(context)

    response = client.get("/v1/graph")

    assert response.status_code == 200
    assert len(response.content) < GRAPH_PAYLOAD_BUDGET_BYTES
    body = response.json()
    returned = ids(body["entities"])
    assert set(seeded["catalog"]) | set(seeded["visible"]) <= returned
    # Only similarities between returned vaults: the visible peers.
    assert {item["candidate_entity_id"] for item in body["similarities"]} == set(
        seeded["peers"]
    )
    incidents = body["active_incidents"]
    assert len(incidents) == 50
    assert all(item["source_entity_id"] in returned for item in incidents)
    # The CRITICAL incidents lead, each with its revealed paths.
    assert [item["severity"] for item in incidents[:20]] == ["CRITICAL"] * 20
    for item in incidents[:20]:
        targets = {
            impact.target_entity_id
            for impact in context.incidents.impacts(item["incident_id"])
        }
        assert targets <= returned
    assert all(
        relationship["source_entity_id"] in returned
        and relationship["target_entity_id"] in returned
        for relationship in body["relationships"]
    )
