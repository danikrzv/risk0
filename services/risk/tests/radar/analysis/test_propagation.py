from datetime import UTC, datetime
from decimal import Decimal

from app.radar.analysis.propagation import PropagationEngine
from app.radar.contracts import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    RadarEntity,
    RadarRelationship,
    SimilarityRecord,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def entity(entity_type: str, key: str, *, visible: bool = True) -> RadarEntity:
    return RadarEntity(
        entity_id=f"{entity_type}:1:{key}",
        entity_type=entity_type,
        chain_id=1,
        address_or_key=key,
        label=key,
        source="fixture",
        visible=visible,
        metadata={},
    )


def relationship(source: RadarEntity, target: RadarEntity, kind: str, weight: str | None = None):
    return RadarRelationship(
        source_entity_id=source.entity_id,
        target_entity_id=target.entity_id,
        relationship_type=kind,
        weight=Decimal(weight) if weight is not None else None,
        metadata={},
    )


def incident(source: RadarEntity) -> Incident:
    return Incident(
        incident_id="incident:test",
        source_entity_id=source.entity_id,
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.OPEN,
        trigger_metrics={},
        rule_version="test-v1",
        first_observed_at=NOW,
        last_observed_at=NOW,
        reasons=("test incident",),
    )


def similarity(catalog: RadarEntity, candidate: RadarEntity, score: str) -> SimilarityRecord:
    return SimilarityRecord(
        catalog_entity_id=catalog.entity_id,
        candidate_entity_id=candidate.entity_id,
        score=Decimal(score),
        components={},
        stored=True,
        normally_visible=False,
        computed_at=NOW,
    )


def test_oracle_incident_propagates_to_market_vault_hidden_neighbor_and_position():
    oracle = entity("oracle", "oracle")
    market = entity("market", "market")
    catalog = entity("vault", "catalog")
    hidden = entity("vault", "hidden", visible=False)
    position = entity("position", "exposure")
    unrelated = entity("vault", "unrelated", visible=False)
    relationships = (
        relationship(market, oracle, "uses_oracle"),
        relationship(catalog, market, "allocated_to", "0.60"),
        relationship(hidden, market, "allocated_to", "0.40"),
        relationship(position, hidden, "lends"),
        relationship(market, catalog, "backed_by"),
    )

    impacts = PropagationEngine().propagate(
        incident(oracle),
        (oracle, market, catalog, hidden, position, unrelated),
        relationships,
        (similarity(catalog, hidden, "0.48"), similarity(catalog, unrelated, "0.40")),
    )

    by_target = {impact.target_entity_id: impact for impact in impacts}
    assert by_target[hidden.entity_id].path == (
        oracle.entity_id,
        market.entity_id,
        hidden.entity_id,
    )
    assert by_target[hidden.entity_id].revealed is True
    assert by_target[position.entity_id].distance == 3
    assert unrelated.entity_id not in by_target


def test_asset_and_chain_incidents_follow_reverse_dependency_edges():
    asset = entity("asset", "usdc")
    chain = entity("chain", "ethereum")
    market = entity("market", "market")
    protocol = entity("protocol", "morpho")
    vault = entity("vault", "vault")
    relationships = (
        relationship(market, asset, "lends"),
        relationship(vault, market, "allocated_to", "1"),
        relationship(protocol, chain, "deployed_on"),
        relationship(market, protocol, "deployed_on"),
    )
    entities = (asset, chain, market, protocol, vault)

    asset_targets = {
        item.target_entity_id
        for item in PropagationEngine().propagate(incident(asset), entities, relationships, ())
    }
    chain_targets = {
        item.target_entity_id
        for item in PropagationEngine().propagate(incident(chain), entities, relationships, ())
    }

    assert {market.entity_id, vault.entity_id}.issubset(asset_targets)
    assert {protocol.entity_id, market.entity_id, vault.entity_id}.issubset(chain_targets)


def test_shortest_path_wins_and_cycles_do_not_repeat_entities():
    oracle = entity("oracle", "oracle")
    market = entity("market", "market")
    vault = entity("vault", "vault")
    relationships = (
        relationship(market, oracle, "uses_oracle"),
        relationship(vault, market, "allocated_to", "1"),
        relationship(vault, oracle, "uses_oracle"),
        relationship(market, vault, "backed_by"),
    )

    impact = next(
        item
        for item in PropagationEngine().propagate(
            incident(oracle), (oracle, market, vault), relationships, ()
        )
        if item.target_entity_id == vault.entity_id
    )

    assert impact.path == (oracle.entity_id, vault.entity_id)
    assert impact.alternate_path_count >= 1
