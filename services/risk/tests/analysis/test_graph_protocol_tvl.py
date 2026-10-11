"""Protocol-node TVL metrics and concentration warnings (DeFiLlama)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.contracts.models import FreshnessStatus, RiskNodeType
from app.domain.graph import node_id
from app.domain.protocol import ProtocolTvlSnapshot
from app.domain.snapshots import DataEnvelope
from tests.analysis.test_graph_builder import (
    build,
    market_context,
    market_lender_position,
    vault_context,
    vault_lender_position,
)


TVL_FETCHED_AT = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
TVL_SOURCE = "defillama https://api.llama.fi/protocols; defillama https://api.llama.fi/v2/chains"
MORPHO = node_id(RiskNodeType.PROTOCOL, "morpho")


def tvl(*, share="0.100259", chain_tvl="5000000000", borrowed="2500000000", freshness=FreshnessStatus.FRESH):
    snapshot = ProtocolTvlSnapshot(
        protocol="morpho",
        label="Morpho Blue",
        chain="Ethereum",
        tvl_usd=Decimal("11000000000"),
        chain_tvl_usd=Decimal(chain_tvl),
        chain_borrowed_usd=None if borrowed is None else Decimal(borrowed),
        chain_defi_tvl_usd=Decimal("50000000000"),
        chain_tvl_share=None if share is None else Decimal(share),
    )
    return DataEnvelope[ProtocolTvlSnapshot](
        value=snapshot, source=TVL_SOURCE, fetched_at=TVL_FETCHED_AT, freshness=freshness
    )


def with_tvl(context, envelope):
    return replace(context, protocol_tvl=envelope)


def test_without_tvl_the_protocol_node_is_unchanged():
    graph = build(market_lender_position(), market_context())

    node = {item.node_id: item for item in graph.nodes}[MORPHO]
    assert node.metrics == {"name": "Morpho Blue", "protocol": "morpho"}
    assert node.provenance == "risk0 curated catalog"
    assert not any("TVL concentration" in warning for warning in graph.completeness_warnings)


def test_market_entry_reports_chain_share_and_its_share_of_gross_deposits():
    graph = build(market_lender_position(), with_tvl(market_context(), tvl()))

    node = {item.node_id: item for item in graph.nodes}[MORPHO]
    assert node.metrics["tvl_usd"] == Decimal("11000000000")
    assert node.metrics["tvl_chain"] == "Ethereum"
    assert node.metrics["chain_tvl_usd"] == Decimal("5000000000")
    assert node.metrics["chain_borrowed_usd"] == Decimal("2500000000")
    assert node.metrics["chain_defi_tvl_usd"] == Decimal("50000000000")
    assert node.metrics["chain_tvl_share"] == Decimal("0.100259")
    # The synthetic market supplies $10M of $7.5B gross Morpho deposits on Ethereum.
    assert node.metrics["entity_tvl_usd"] == Decimal("10000000")
    assert node.metrics["entity_share_of_protocol_tvl"] == Decimal("0.001333")
    assert node.provenance == f"risk0 curated catalog; TVL: {TVL_SOURCE}"
    assert node.freshness is FreshnessStatus.FRESH
    assert node.observed_at == TVL_FETCHED_AT


def test_vault_entry_uses_vault_total_assets():
    graph = build(vault_lender_position(), with_tvl(vault_context(), tvl()))

    node = {item.node_id: item for item in graph.nodes}[MORPHO]
    assert node.metrics["entity_tvl_usd"] == Decimal("10000000")
    assert node.metrics["entity_share_of_protocol_tvl"] == Decimal("0.001333")


def test_chain_share_at_the_threshold_is_a_concentration_warning():
    graph = build(market_lender_position(), with_tvl(market_context(), tvl(share="0.05")))

    (warning,) = [item for item in graph.completeness_warnings if "TVL concentration" in item]
    assert warning.startswith("TVL concentration: Morpho Blue holds 5.0% of Ethereum DeFi TVL")
    assert "threshold 5.0%" in warning


def test_chain_share_below_the_threshold_is_not_a_warning():
    graph = build(market_lender_position(), with_tvl(market_context(), tvl(share="0.049999")))

    assert not any("TVL concentration" in item for item in graph.completeness_warnings)


def test_entity_holding_a_large_share_of_protocol_deposits_is_a_warning():
    # $10M market against $100M gross deposits -> 10%.
    envelope = tvl(share="0.01", chain_tvl="60000000", borrowed="40000000")

    graph = build(market_lender_position(), with_tvl(market_context(), envelope))

    node = {item.node_id: item for item in graph.nodes}[MORPHO]
    assert node.metrics["entity_share_of_protocol_tvl"] == Decimal("0.1")
    (warning,) = [item for item in graph.completeness_warnings if "TVL concentration" in item]
    assert "is 10.0% of Morpho Blue's gross Ethereum deposits (threshold 10.0%)" in warning


def test_unknown_borrowed_or_share_leaves_metrics_unknown_without_warnings():
    graph = build(
        market_lender_position(),
        with_tvl(market_context(), tvl(share=None, borrowed=None, freshness=FreshnessStatus.STALE_USABLE)),
    )

    node = {item.node_id: item for item in graph.nodes}[MORPHO]
    assert node.metrics["chain_tvl_share"] is None
    assert node.metrics["entity_share_of_protocol_tvl"] is None
    assert node.freshness is FreshnessStatus.STALE_USABLE
    assert not any("TVL concentration" in item for item in graph.completeness_warnings)
