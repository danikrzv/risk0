"""Decoded oracles: feeds appear as oracle nodes under the market oracle."""

from dataclasses import replace
from decimal import Decimal

from app.contracts.models import RiskEdgeType, RiskNodeType
from app.domain.graph import OracleFeedObservation, OracleObservation, node_id
from tests.analysis.test_graph_builder import (
    ORACLE_A,
    ORACLE_B,
    ORACLE_SOURCE,
    borrower_position,
    build,
    envelope,
    market_context,
    nodes_by_id,
    vault_context,
    vault_lender_position,
)


ETH_FEED = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419"
USDC_FEED = "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6"


def feed(address: str, description: str, *, staleness: str = "0", heartbeat: str = "3600") -> OracleFeedObservation:
    return OracleFeedObservation(
        role="BASE_FEED_1",
        address=address,
        kind="chainlink",
        description=description,
        answer=Decimal("2500"),
        last_update_age_seconds=Decimal("120"),
        heartbeat_seconds=Decimal(heartbeat),
        heartbeat_source="documented (ETH/USD)",
        staleness_seconds=Decimal(staleness),
    )


def decoded(address: str, *feeds: OracleFeedObservation, deviation: str = "0.001") -> OracleObservation:
    return OracleObservation(
        oracle_address=address,
        age_seconds=max((item.staleness_seconds for item in feeds), default=Decimal(0)),
        deviation=Decimal(deviation),
        oracle_type="MorphoChainlinkOracleV2",
        feeds=feeds,
    )


def test_feeds_are_oracle_nodes_linked_from_the_market_oracle():
    observation = decoded(ORACLE_A, feed(ETH_FEED, "ETH / USD", staleness="30"), feed(USDC_FEED, "USDC / USD", heartbeat="86400"))
    context = replace(market_context(), oracles={ORACLE_A: envelope(observation, ORACLE_SOURCE)})

    graph = build(borrower_position(), context)

    nodes = nodes_by_id(graph)
    oracle = nodes[node_id(RiskNodeType.ORACLE, ORACLE_A)]
    assert oracle.metrics["oracle_type"] == "MorphoChainlinkOracleV2"
    assert oracle.metrics["decomposed"] is True
    assert oracle.metrics["feed_count"] == Decimal(2)
    eth = nodes[node_id(RiskNodeType.ORACLE, ETH_FEED)]
    assert eth.label == "ETH / USD (chainlink)"
    assert eth.metrics["description"] == "ETH / USD"
    assert eth.metrics["heartbeat_seconds"] == Decimal(3600)
    assert eth.metrics["last_update_age_seconds"] == Decimal(120)
    assert eth.metrics["age_seconds"] == Decimal(30)
    assert eth.metrics["deviation"] == Decimal("0.001")
    assert eth.provenance == "https://rpc.test/oracle-a"
    edges = {(edge.source, edge.target, edge.relationship) for edge in graph.edges}
    assert (oracle.node_id, eth.node_id, RiskEdgeType.USES_ORACLE) in edges
    assert (oracle.node_id, node_id(RiskNodeType.ORACLE, USDC_FEED), RiskEdgeType.USES_ORACLE) in edges


def test_an_oracle_without_decomposition_has_no_feed_nodes():
    graph = build(borrower_position(), market_context())

    nodes = nodes_by_id(graph)
    oracle = nodes[node_id(RiskNodeType.ORACLE, ORACLE_A)]
    assert oracle.metrics["decomposed"] is False
    assert [node for node in graph.nodes if node.node_type is RiskNodeType.ORACLE] == [oracle]


def test_a_feed_shared_by_two_oracles_is_one_node_with_the_largest_deviation():
    context = vault_context()
    oracles = {
        ORACLE_A: envelope(decoded(ORACLE_A, feed(ETH_FEED, "ETH / USD"), deviation="0.002"), ORACLE_SOURCE),
        ORACLE_B: envelope(decoded(ORACLE_B, feed(ETH_FEED, "ETH / USD"), deviation="0.004"), ORACLE_SOURCE),
    }

    graph = build(vault_lender_position(), replace(context, oracles=oracles))

    shared = [node for node in graph.nodes if node.node_id == node_id(RiskNodeType.ORACLE, ETH_FEED)]
    assert len(shared) == 1
    assert shared[0].metrics["deviation"] == Decimal("0.004")
    parents = {
        edge.source for edge in graph.edges if edge.target == shared[0].node_id and edge.relationship is RiskEdgeType.USES_ORACLE
    }
    assert parents == {node_id(RiskNodeType.ORACLE, ORACLE_A), node_id(RiskNodeType.ORACLE, ORACLE_B)}
