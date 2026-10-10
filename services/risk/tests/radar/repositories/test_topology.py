import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from app.radar.contracts import (
    RadarEntityType,
    RadarRelationship,
    RadarRelationshipType,
    SimilarityRecord,
)
from tests.radar.repositories.builders import (
    CATALOG_VAULT,
    NEIGHBOR_VAULT,
    NOW,
    entity,
    incident,
    snapshot,
)


MARKET_KEY = "0x" + "ab" * 32
MARKET = f"market:1:{MARKET_KEY}"


@pytest.fixture
def topology(database):
    from app.radar.repositories import TopologyRepository

    return TopologyRepository(database)


def seed(topology):
    topology.upsert_entities(
        (
            entity(catalog=True, metadata={"curator": "rockawayx", "tvl_usd": Decimal("1.50")}),
            entity(key="0x2222222222222222222222222222222222222222", visible=False),
            entity(RadarEntityType.MARKET, MARKET_KEY),
            entity(RadarEntityType.VAULT, "0x9999999999999999999999999999999999999999", chain_id=8453),
        ),
        seen_at=NOW,
    )


def test_upserted_entities_round_trip_with_exact_metadata_types(topology):
    seed(topology)

    stored = topology.get_entity(CATALOG_VAULT)

    assert stored is not None
    assert stored.catalog is True
    assert stored.metadata == {"curator": "rockawayx", "tvl_usd": Decimal("1.50")}
    assert isinstance(stored.metadata["tvl_usd"], Decimal)
    assert topology.get_entity("vault:1:0xunknown") is None


def test_entities_are_filtered_by_chain_type_and_visibility(topology):
    seed(topology)

    assert [e.entity_id for e in topology.entities(chain_id=8453)] == [
        "vault:8453:0x9999999999999999999999999999999999999999"
    ]
    assert [e.entity_id for e in topology.entities(entity_type=RadarEntityType.MARKET)] == [
        MARKET
    ]
    assert NEIGHBOR_VAULT not in {e.entity_id for e in topology.entities(visible=True)}
    assert [e.entity_id for e in topology.entities(visible=False)] == [NEIGHBOR_VAULT]
    assert [e.entity_id for e in topology.entities(entity_ids=(MARKET, CATALOG_VAULT))] == [
        MARKET,
        CATALOG_VAULT,
    ]


def test_rediscovered_entity_updates_metadata_without_losing_history(database, topology):
    from app.radar.repositories import IncidentRepository, StateRepository

    seed(topology)
    states = StateRepository(database)
    incidents = IncidentRepository(database)
    assert states.append_snapshot(snapshot(CATALOG_VAULT))
    incidents.upsert_active(incident(source_entity_id=CATALOG_VAULT))

    later = NOW + timedelta(hours=1)
    topology.upsert_entities(
        (entity(catalog=True, label="Renamed vault", metadata={"curator": "new-curator"}),),
        seen_at=later,
    )

    stored = topology.get_entity(CATALOG_VAULT)
    assert stored.label == "Renamed vault"
    assert stored.metadata == {"curator": "new-curator"}
    assert topology.first_seen_at(CATALOG_VAULT) == NOW
    assert states.latest_snapshot(CATALOG_VAULT) is not None
    assert incidents.active(source_entity_id=CATALOG_VAULT)


def test_missing_entities_are_tombstoned_only_on_successfully_discovered_chains(topology):
    seed(topology)
    later = NOW + timedelta(hours=1)

    tombstoned = topology.tombstone_missing(
        seen_entity_ids=(CATALOG_VAULT, MARKET),
        chain_ids=(1,),
        at=later,
    )

    assert tombstoned == 1
    assert topology.get_entity(NEIGHBOR_VAULT) is None
    assert topology.get_entity(NEIGHBOR_VAULT, include_tombstoned=True) is not None
    assert topology.get_entity("vault:8453:0x9999999999999999999999999999999999999999")

    topology.upsert_entities(
        (entity(key="0x2222222222222222222222222222222222222222"),),
        seen_at=later + timedelta(hours=1),
    )
    assert topology.get_entity(NEIGHBOR_VAULT) is not None


def test_relationships_are_upserted_without_duplicates(topology):
    seed(topology)
    relationship = RadarRelationship(
        source_entity_id=CATALOG_VAULT,
        target_entity_id=MARKET,
        relationship_type=RadarRelationshipType.ALLOCATED_TO,
        weight=Decimal("0.40"),
    )

    topology.upsert_relationships((relationship,), seen_at=NOW)
    topology.upsert_relationships(
        (relationship.model_copy(update={"weight": Decimal("0.55")}),),
        seen_at=NOW + timedelta(minutes=5),
    )

    stored = topology.relationships()
    assert len(stored) == 1
    assert stored[0].weight == Decimal("0.55")
    assert topology.relationships(entity_ids=(NEIGHBOR_VAULT,)) == ()
    assert topology.relationships(
        relationship_type=RadarRelationshipType.ALLOCATED_TO, entity_ids=(MARKET,)
    ) == stored


def test_relationships_require_known_entities(topology):
    seed(topology)

    with pytest.raises(sqlite3.IntegrityError):
        topology.upsert_relationships(
            (
                RadarRelationship(
                    source_entity_id=CATALOG_VAULT,
                    target_entity_id="market:1:0xmissing",
                    relationship_type=RadarRelationshipType.ALLOCATED_TO,
                ),
            ),
            seen_at=NOW,
        )


def similarity(candidate: str, score: str, *, stored: bool = True) -> SimilarityRecord:
    return SimilarityRecord(
        catalog_entity_id=CATALOG_VAULT,
        candidate_entity_id=candidate,
        score=Decimal(score),
        components={"shared_markets": Decimal("0.30"), "same_chain": Decimal("0.05")},
        missing_dimensions=("oracle_overlap",),
        stored=stored,
        normally_visible=Decimal(score) > Decimal("0.60"),
        computed_at=NOW,
    )


def test_similarities_are_replaced_per_catalog_vault_and_only_stored_records_persist(
    topology,
):
    seed(topology)
    third = "vault:1:0x3333333333333333333333333333333333333333"
    topology.upsert_entities((entity(key=third.rsplit(":", 1)[1]),), seen_at=NOW)

    topology.replace_similarities(CATALOG_VAULT, (similarity(NEIGHBOR_VAULT, "0.48"),))
    topology.replace_similarities(
        CATALOG_VAULT,
        (
            similarity(third, "0.72"),
            similarity(NEIGHBOR_VAULT, "0.20", stored=False),
        ),
    )

    stored = topology.similarities(catalog_entity_id=CATALOG_VAULT)
    assert [record.candidate_entity_id for record in stored] == [third]
    assert stored[0].score == Decimal("0.72")
    assert stored[0].components["shared_markets"] == Decimal("0.30")
    assert stored[0].missing_dimensions == ("oracle_overlap",)
    assert stored[0].normally_visible is True
    assert topology.similarities(candidate_entity_id=NEIGHBOR_VAULT) == ()
