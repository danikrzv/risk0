"""Issuer and underlying look-through in the risk graph and its scenario propagation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analysis.graph_builder import build_risk_graph
from app.analysis.scenario_engine import ScenarioEngine
from app.catalog.assets import (
    MAX_UNDERLYING_DEPTH,
    AssetRegistry,
    ClaimSources,
    Issuer,
    IssuerType,
    RegisteredAsset,
    Underlying,
    load_asset_registry,
)
from app.contracts.models import (
    CatalogEntityType,
    CatalogEntry,
    CatalogProvenance,
    FreshnessStatus,
    PositionInput,
    PositionMode,
    RiskEdgeType,
    RiskGraph,
    RiskNodeType,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.graph import GraphContext, OracleObservation
from app.domain.position import validate_position
from app.domain.snapshots import AssetInfo, DataEnvelope, MarketSnapshot


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
WSTETH_WETH_MARKET = "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e"
WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"
STETH = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
ORACLE = "0x00000000000000000000000000000000000000c1"
UNREGISTERED = "0x00000000000000000000000000000000000000a1"
WRAPPED_USDC = "0x00000000000000000000000000000000000000a2"

POSITION_ID = "position:1:borrower-wsteth-weth-965"
MARKET_ID = f"market:1:{WSTETH_WETH_MARKET}"
WSTETH_ID = f"asset:1:{WSTETH.lower()}"
STETH_ID = f"asset:1:{STETH.lower()}"
WETH_ID = f"asset:1:{WETH.lower()}"
USDC_ID = f"asset:1:{USDC.lower()}"
ETH_ID = "asset:1:underlying-eth"
LIDO_ID = "protocol:1:issuer-lido"
WETH9_ID = "protocol:1:issuer-weth9"
ETHEREUM_ID = "chain:1:ethereum"


def asset(address: str, symbol: str, price: str) -> AssetInfo:
    return AssetInfo(address=address, symbol=symbol, decimals=18, price_usd=Decimal(price))


def entry(entity_id: str, market_id: str) -> CatalogEntry:
    return CatalogEntry(
        entity_id=entity_id,
        chain_id=1,
        entity_type=CatalogEntityType.MORPHO_MARKET,
        address_or_market_id=market_id,
        name=entity_id,
        description="synthetic",
        provenance=CatalogProvenance.REPRESENTATIVE_PROXY,
        provenance_label="synthetic",
        supported=True,
        supported_modes=(PositionMode.LENDER, PositionMode.BORROWER),
        lltv=None,
    )


def market(market_id: str, loan: AssetInfo, collateral: AssetInfo, lltv: str) -> MarketSnapshot:
    return MarketSnapshot(
        chain_id=1,
        market_id=market_id,
        loan_asset=loan,
        collateral_asset=collateral,
        oracle_address=ORACLE,
        irm_address="0x000000000000000000000000000000000000cafe",
        lltv=Decimal(lltv),
        total_supply_assets=Decimal("1000"),
        total_borrow_assets=Decimal("800"),
        liquidity_assets=Decimal("200"),
        utilization=Decimal("0.8"),
        total_supply_usd=Decimal("2500000"),
        total_borrow_usd=Decimal("2000000"),
        liquidity_usd=Decimal("500000"),
        listed=True,
        last_accrual_timestamp=1_791_000_000,
        sources=("https://morpho.test/market",),
    )


def envelope(value) -> DataEnvelope:
    return DataEnvelope(
        value=value,
        source="https://morpho.test/market",
        fetched_at=NOW,
        freshness=FreshnessStatus.FRESH,
        block_number=1,
    )


def wsteth_case(registry: AssetRegistry | None, mode: PositionMode = PositionMode.BORROWER):
    snapshot = market(
        WSTETH_WETH_MARKET, asset(WETH, "WETH", "2500"), asset(WSTETH, "wstETH", "3000"), "0.965"
    )
    catalog_entry = entry("wsteth-weth-965", WSTETH_WETH_MARKET)
    if mode is PositionMode.BORROWER:
        position_input = PositionInput(
            mode=mode,
            entity_id="wsteth-weth-965",
            amount=Decimal("10.8"),
            collateral_amount=Decimal("10"),
            debt_amount=Decimal("10.8"),
            ltv=Decimal("0.90"),
            numeraire="WETH",
        )
    else:
        position_input = PositionInput(
            mode=mode, entity_id="wsteth-weth-965", amount=Decimal("10"), numeraire="WETH"
        )
    position = validate_position(position_input, catalog_entry, snapshot, now=NOW)
    context = GraphContext(
        entry=catalog_entry,
        market=envelope(snapshot),
        oracles={ORACLE: envelope(OracleObservation(oracle_address=ORACLE, age_seconds=Decimal(60), deviation=Decimal("0.001")))},
        asset_registry=registry,
    )
    return position, build_risk_graph(position, context, clock=lambda: NOW)


def edges(graph: RiskGraph) -> set[tuple[str, str, RiskEdgeType]]:
    return {(edge.source, edge.target, edge.relationship) for edge in graph.edges}


def nodes(graph: RiskGraph):
    return {node.node_id: node for node in graph.nodes}


def run(position, graph, scenario: ScenarioId, severity: str):
    selection = ScenarioSelection(scenario_id=scenario, severity=Decimal(severity))
    return ScenarioEngine().run(position, graph, (selection,), {})[0]


# Graph shape


def test_wsteth_borrower_graph_reaches_steth_and_lido():
    _, graph = wsteth_case(load_asset_registry())

    graph_nodes = nodes(graph)
    assert {STETH_ID, ETH_ID, LIDO_ID, WETH9_ID} <= set(graph_nodes)
    assert {
        (WSTETH_ID, STETH_ID, RiskEdgeType.BACKED_BY),
        (WSTETH_ID, LIDO_ID, RiskEdgeType.BACKED_BY),
        (STETH_ID, LIDO_ID, RiskEdgeType.BACKED_BY),
        (STETH_ID, ETH_ID, RiskEdgeType.BACKED_BY),
        (WETH_ID, ETH_ID, RiskEdgeType.BACKED_BY),
        (WETH_ID, WETH9_ID, RiskEdgeType.BACKED_BY),
        (LIDO_ID, ETHEREUM_ID, RiskEdgeType.DEPLOYED_ON),
        (WETH9_ID, ETHEREUM_ID, RiskEdgeType.DEPLOYED_ON),
        (ETH_ID, ETHEREUM_ID, RiskEdgeType.DEPLOYED_ON),
    } <= edges(graph)

    lido = graph_nodes[LIDO_ID]
    assert lido.node_type is RiskNodeType.PROTOCOL
    assert lido.label == "Lido"
    assert lido.metrics["issuer_type"] == "protocol"
    assert lido.metrics["defiscan_slug"] == "lido"
    assert str(lido.metrics["source_url"]).startswith("https://docs.lido.fi/")
    assert "withdrawal" in str(lido.metrics["redemption"]).lower()
    assert lido.provenance == "risk0 asset registry"

    steth = graph_nodes[STETH_ID]
    assert steth.node_type is RiskNodeType.ASSET
    assert steth.metrics["symbol"] == "stETH"
    assert steth.metrics["price_usd"] is None  # registry-only node: no market data is invented

    wsteth = graph_nodes[WSTETH_ID]
    assert wsteth.metrics["issuer"] == "Lido"
    assert wsteth.metrics["price_usd"] == Decimal("3000")  # snapshot metrics are kept
    assert "unwrap" in str(wsteth.metrics["redemption"])
    assert graph_nodes[ETH_ID].metrics["underlying_kind"] == "native"
    assert not any("asset registry" in warning for warning in graph.completeness_warnings)


def test_without_a_registry_the_graph_is_unchanged():
    _, with_none = wsteth_case(None)

    assert not any(edge.relationship is RiskEdgeType.BACKED_BY for edge in with_none.edges)
    assert LIDO_ID not in nodes(with_none)


def _registry_without(symbol: str) -> AssetRegistry:
    registry = load_asset_registry()
    return AssetRegistry.model_validate(
        {
            **registry.model_dump(mode="json"),
            "assets": [
                item.model_dump(mode="json")
                for item in registry.assets
                if item.symbol not in {symbol}
            ],
        }
    )


def test_an_unregistered_asset_gets_no_new_nodes_and_a_warning():
    _, graph = wsteth_case(_registry_without("wstETH"))

    assert not any(edge.source == WSTETH_ID and edge.relationship is RiskEdgeType.BACKED_BY for edge in graph.edges)
    assert STETH_ID not in nodes(graph)
    assert LIDO_ID not in nodes(graph)
    assert any(
        "wstETH" in warning and "unmodelled, not assumed safe" in warning
        for warning in graph.completeness_warnings
    )
    # The registered side is still looked through.
    assert (WETH_ID, WETH9_ID, RiskEdgeType.BACKED_BY) in edges(graph)


# Cycle and depth safety (registries built without validation)


def _issuer() -> Issuer:
    return Issuer.model_construct(
        issuer_id="synthetic",
        name="Synthetic issuer",
        issuer_type=IssuerType.PROTOCOL,
        redemption="synthetic",
        defiscan_slug=None,
        source_urls=("https://issuer.test",),
    )


def _registered(address: str, symbol: str, underlying: str) -> RegisteredAsset:
    sources = ClaimSources.model_construct(
        underlying=("https://x.test",), issuer=("https://x.test",), redemption=("https://x.test",)
    )
    return RegisteredAsset.model_construct(
        address=address,
        symbol=symbol,
        underlying=Underlying.model_construct(asset=underlying, reference=None),
        issuer_id="synthetic",
        redemption="synthetic",
        sources=sources,
    )


def _unchecked_registry(*assets: RegisteredAsset) -> AssetRegistry:
    return AssetRegistry.model_construct(
        schema_version=1,
        chain_id=1,
        reviewed_at="2026-10-11",
        description="unchecked",
        issuers=(_issuer(),),
        references=(),
        assets=assets,
    )


def test_an_underlying_cycle_is_cut_and_reported():
    registry = _unchecked_registry(
        _registered(WSTETH, "wstETH", STETH),
        _registered(STETH, "stETH", WSTETH),
        _registered(WETH, "WETH", WSTETH),
    )
    position, graph = wsteth_case(registry)

    built = edges(graph)
    assert (WSTETH_ID, STETH_ID, RiskEdgeType.BACKED_BY) in built
    assert (STETH_ID, WSTETH_ID, RiskEdgeType.BACKED_BY) not in built
    assert any("cycle" in warning for warning in graph.completeness_warnings)
    # The engine rejects cyclic graphs; this one must still run.
    for scenario, severity in ((ScenarioId.PROTOCOL_IMPAIRMENT, "0.10"), (ScenarioId.COLLATERAL_SHOCK, "0.15")):
        assert run(position, graph, scenario, severity).affected_path


def test_underlying_depth_is_bounded():
    chain = [WSTETH] + [f"0x{index:040x}" for index in range(1, MAX_UNDERLYING_DEPTH + 4)]
    registry = _unchecked_registry(
        *(
            _registered(address, f"T{index}", chain[index + 1])
            for index, address in enumerate(chain[:-1])
        )
    )
    _, graph = wsteth_case(registry)

    backed = [edge for edge in graph.edges if edge.target.startswith("asset:") and edge.relationship is RiskEdgeType.BACKED_BY]
    assert len(backed) == MAX_UNDERLYING_DEPTH
    assert any("exceeds" in warning for warning in graph.completeness_warnings)


# Propagation


def test_protocol_impairment_path_reaches_the_issuer():
    position, graph = wsteth_case(load_asset_registry())

    result = run(position, graph, ScenarioId.PROTOCOL_IMPAIRMENT, "0.10")

    path = result.affected_path
    assert path[0] == POSITION_ID
    assert {LIDO_ID, WETH9_ID, STETH_ID, WSTETH_ID} <= set(path)
    # The collateral impairment itself is unchanged by look-through.
    _, plain = wsteth_case(None)
    assert result.impact == run(position, plain, ScenarioId.PROTOCOL_IMPAIRMENT, "0.10").impact


def test_lender_protocol_impairment_path_reaches_the_issuer():
    position, graph = wsteth_case(load_asset_registry(), PositionMode.LENDER)

    result = run(position, graph, ScenarioId.PROTOCOL_IMPAIRMENT, "0.10")

    assert {LIDO_ID, STETH_ID} <= set(result.affected_path)
    assert result.stressed_values["exposed_fraction"] == Decimal(1)


def test_collateral_shock_is_unchanged_by_look_through():
    position, graph = wsteth_case(load_asset_registry())
    _, plain = wsteth_case(None)

    shocked = run(position, graph, ScenarioId.COLLATERAL_SHOCK, "0.15")
    baseline = run(position, plain, ScenarioId.COLLATERAL_SHOCK, "0.15")

    assert shocked.impact == baseline.impact
    assert shocked.stressed_values["stressed_debt_value"] == baseline.stressed_values["stressed_debt_value"]


# A wrapper of USDC used as collateral: a USDC depeg reaches it only through backed_by.

WRAPPED_MARKET = "0x" + "ab" * 32


def _wrapped_usdc_registry() -> AssetRegistry:
    registry = load_asset_registry()
    wrapper = {
        "address": WRAPPED_USDC,
        "symbol": "wUSDC",
        "underlying": {"asset": USDC},
        "issuer_id": "steakhouse-financial",
        "redemption": "synthetic ERC-4626 redeem",
        "sources": {
            "underlying": ["https://x.test"],
            "issuer": ["https://x.test"],
            "redemption": ["https://x.test"],
        },
    }
    payload = registry.model_dump(mode="json")
    payload["assets"].append(wrapper)
    return AssetRegistry.model_validate(payload)


def wrapped_case(registry: AssetRegistry | None, mode: PositionMode):
    snapshot = market(
        WRAPPED_MARKET, asset(WETH, "WETH", "2500"), asset(WRAPPED_USDC, "wUSDC", "1"), "0.86"
    )
    catalog_entry = entry("wusdc-weth", WRAPPED_MARKET)
    if mode is PositionMode.BORROWER:
        position_input = PositionInput(
            mode=mode,
            entity_id="wusdc-weth",
            amount=Decimal("20"),
            collateral_amount=Decimal("100000"),
            debt_amount=Decimal("20"),
            ltv=Decimal("0.5"),
            numeraire="WETH",
        )
    else:
        position_input = PositionInput(mode=mode, entity_id="wusdc-weth", amount=Decimal("10"), numeraire="WETH")
    position = validate_position(position_input, catalog_entry, snapshot, now=NOW)
    context = GraphContext(entry=catalog_entry, market=envelope(snapshot), asset_registry=registry)
    return position, build_risk_graph(position, context, clock=lambda: NOW)


def test_borrower_depeg_reaches_collateral_through_backed_by():
    position, graph = wrapped_case(_wrapped_usdc_registry(), PositionMode.BORROWER)
    _, plain = wrapped_case(None, PositionMode.BORROWER)

    looked_through = run(position, graph, ScenarioId.STABLECOIN_DEPEG, "0.10")
    baseline = run(position, plain, ScenarioId.STABLECOIN_DEPEG, "0.10")

    collateral = looked_through.stressed_values["collateral_value"]
    assert looked_through.stressed_values["stressed_collateral_value"] == collateral * Decimal("0.90")
    assert looked_through.impact > 0
    assert USDC_ID in looked_through.affected_path
    assert baseline.stressed_values["stressed_collateral_value"] == baseline.stressed_values["collateral_value"]


@pytest.mark.parametrize(("registry", "exposed"), [(True, Decimal(1)), (False, Decimal(0))])
def test_lender_depeg_exposure_follows_backed_by(registry, exposed):
    position, graph = wrapped_case(_wrapped_usdc_registry() if registry else None, PositionMode.LENDER)

    result = run(position, graph, ScenarioId.STABLECOIN_DEPEG, "0.10")

    assert result.stressed_values["exposed_fraction"] == exposed
