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


def test_exits_through_and_zero_weight_edges_do_not_propagate():
    asset = entity("asset", "usdc")
    pool = entity("protocol", "pool")
    position = entity("position", "exit")
    market = entity("market", "market")
    emptied = entity("vault", "emptied")
    relationships = (
        relationship(pool, asset, "exits_through"),
        relationship(position, pool, "exits_through"),
        relationship(market, asset, "uses_collateral"),
        relationship(emptied, market, "allocated_to", "0"),
    )

    targets = {
        item.target_entity_id
        for item in PropagationEngine().propagate(
            incident(asset), (asset, pool, position, market, emptied), relationships, ()
        )
    }

    assert targets == {market.entity_id}


def test_incident_originating_at_hidden_neighbor_reveals_it():
    catalog = entity("vault", "catalog")
    hidden = entity("vault", "hidden", visible=False)
    position = entity("position", "exposure")
    unrelated = entity("vault", "unrelated", visible=False)

    impacts = PropagationEngine().propagate(
        incident(hidden),
        (catalog, hidden, position, unrelated),
        (relationship(position, hidden, "lends"),),
        (similarity(catalog, hidden, "0.48"), similarity(catalog, unrelated, "0.48")),
    )

    by_target = {impact.target_entity_id: impact for impact in impacts}
    assert by_target[hidden.entity_id].distance == 0
    assert by_target[hidden.entity_id].revealed is True
    assert by_target[position.entity_id].revealed is True
    assert unrelated.entity_id not in by_target


def test_hidden_neighbor_at_or_below_storage_threshold_is_not_revealed():
    oracle = entity("oracle", "oracle")
    market = entity("market", "market")
    catalog = entity("vault", "catalog")
    hidden = entity("vault", "hidden", visible=False)

    impacts = PropagationEngine().propagate(
        incident(oracle),
        (oracle, market, catalog, hidden),
        (
            relationship(market, oracle, "uses_oracle"),
            relationship(hidden, market, "allocated_to", "0.5"),
        ),
        (similarity(catalog, hidden, "0.33"),),
    )

    assert all(item.revealed is False for item in impacts)


def test_alternate_paths_count_distinct_arrivals_without_back_edges():
    oracle = entity("oracle", "o")
    first = entity("market", "m1")
    second = entity("market", "m2")
    vault = entity("vault", "v")
    relationships = (
        relationship(first, oracle, "uses_oracle"),
        relationship(second, oracle, "uses_oracle"),
        relationship(vault, first, "allocated_to", "0.5"),
        relationship(vault, second, "allocated_to", "0.5"),
        relationship(first, vault, "backed_by"),
    )

    by_target = {
        item.target_entity_id: item
        for item in PropagationEngine().propagate(
            incident(oracle), (oracle, first, second, vault), relationships, ()
        )
    }

    assert by_target[vault.entity_id].alternate_path_count == 1
    # first is reached directly; vault's path runs through first, so it is no alternate
    assert by_target[first.entity_id].alternate_path_count == 0


def test_equal_length_paths_prefer_higher_confidence():
    oracle = entity("oracle", "o")
    market = entity("market", "a-market")
    protocol = entity("protocol", "a-protocol")
    vault = entity("vault", "v")
    relationships = (
        relationship(market, oracle, "uses_oracle"),
        relationship(protocol, oracle, "deployed_on"),
        relationship(vault, protocol, "deployed_on"),
        relationship(vault, market, "allocated_to", "0.5"),
    )

    impact = next(
        item
        for item in PropagationEngine().propagate(
            incident(oracle), (oracle, market, protocol, vault), relationships, ()
        )
        if item.target_entity_id == vault.entity_id
    )

    assert impact.path == (oracle.entity_id, market.entity_id, vault.entity_id)
    assert impact.confidence == Decimal("0.95")


def test_oracle_incident_fixture_is_reproduced_exactly():
    import json
    from pathlib import Path

    from app.radar.analysis.exposure import ExposureImpactEstimator
    from app.radar.contracts import IncidentGraphResponse

    fixture = Path(__file__).parents[2] / "fixtures" / "radar" / "oracle-incident.json"
    graph = IncidentGraphResponse.model_validate(json.loads(fixture.read_text()))
    hidden_id = "vault:1:0x3333333333333333333333333333333333333333"
    # Before the reveal the 48% neighbor is hidden and carries no reveal metadata.
    entities = tuple(
        item.model_copy(update={"visible": False, "metadata": {}})
        if item.entity_id == hidden_id
        else item
        for item in graph.entities
    )

    for candidate_entities in (entities, graph.entities):
        impacts = PropagationEngine().propagate(
            graph.incident, candidate_entities, graph.relationships, graph.similarities
        )
        by_target = {item.target_entity_id: item for item in impacts}
        assert by_target[hidden_id].revealed is True
        assert by_target["vault:1:0x1111111111111111111111111111111111111111"].revealed is False

        result = ExposureImpactEstimator().estimate(
            graph.exposures[0], graph.incident, impacts, graph.relationships
        )
        assert len(result.impacts) == len(graph.impacts) == 1
        expected = graph.impacts[0]
        actual = result.impacts[0]
        assert actual.model_dump(exclude={"reasons"}) == expected.model_dump(exclude={"reasons"})
