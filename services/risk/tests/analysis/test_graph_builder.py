import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.graph_builder import build_risk_graph
from app.analysis.scenario_engine import ScenarioEngine
from app.contracts.models import (
    CatalogEntityType,
    CatalogEntry,
    CatalogProvenance,
    ConfidenceLevel,
    FreshnessStatus,
    PositionInput,
    PositionMode,
    RiskEdgeType,
    RiskGraph,
    RiskNodeType,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.graph import (
    CATALOG_PROVENANCE,
    POSITION_PROVENANCE,
    GraphConstructionError,
    GraphContext,
    OracleObservation,
    node_id,
)
from app.domain.position import validate_position
from app.domain.scenario import SCENARIO_DEFINITIONS
from app.domain.snapshots import (
    AllocationSnapshot,
    AssetInfo,
    DataEnvelope,
    MarketSnapshot,
    PoolSnapshot,
    VaultSnapshot,
)


VALIDATED_AT = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)
FETCHED_AT = datetime(2026, 10, 10, 9, 14, 30, tzinfo=UTC)
BUILT_AT = datetime(2026, 10, 10, 9, 15, 5, tzinfo=UTC)

WETH_USDC_MARKET = "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"
WBTC_USDC_MARKET = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
EMPTY_MARKET = "0x" + "e" * 64
VAULT_ADDRESS = "0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8"
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WBTC_ADDRESS = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
# Synthetic oracle, IRM and pool addresses.
ORACLE_A = "0x00000000000000000000000000000000000000c1"
ORACLE_B = "0x00000000000000000000000000000000000000c2"
IRM = "0x000000000000000000000000000000000000cafe"
POOL_A = "0x00000000000000000000000000000000000000d1"
POOL_B = "0x00000000000000000000000000000000000000d2"

POSITION_BORROWER = "position:1:borrower-weth-usdc-860"
POSITION_LENDER = "position:1:lender-weth-usdc-860"
POSITION_VAULT = "position:1:lender-mev-capital-weth"
MARKET_A_ID = f"market:1:{WETH_USDC_MARKET}"
MARKET_B_ID = f"market:1:{WBTC_USDC_MARKET}"
VAULT_ID = f"vault:1:{VAULT_ADDRESS.lower()}"
WETH_ID = f"asset:1:{WETH_ADDRESS.lower()}"
USDC_ID = f"asset:1:{USDC_ADDRESS.lower()}"
WBTC_ID = f"asset:1:{WBTC_ADDRESS.lower()}"
ORACLE_A_ID = f"oracle:1:{ORACLE_A}"
ORACLE_B_ID = f"oracle:1:{ORACLE_B}"
POOL_A_ID = f"pool:1:{POOL_A}"
POOL_B_ID = f"pool:1:{POOL_B}"
MORPHO_ID = "protocol:1:morpho"
ETHEREUM_ID = "chain:1:ethereum"

NODE_ID_PATTERN = re.compile(r"^(position|vault|market|protocol|asset|oracle|pool|chain):1:[0-9a-z][0-9a-z_.-]*$")


def weth(price: str | None = "2000") -> AssetInfo:
    return AssetInfo(address=WETH_ADDRESS, symbol="WETH", decimals=18, price_usd=_dec(price))


def usdc(price: str | None = "1") -> AssetInfo:
    return AssetInfo(address=USDC_ADDRESS, symbol="USDC", decimals=6, price_usd=_dec(price))


def wbtc(price: str | None = "60000") -> AssetInfo:
    return AssetInfo(address=WBTC_ADDRESS, symbol="WBTC", decimals=8, price_usd=_dec(price))


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def envelope(value, source: str, *, freshness=FreshnessStatus.FRESH, fetched_at=FETCHED_AT):
    return DataEnvelope(value=value, source=source, fetched_at=fetched_at, freshness=freshness, block_number=23_000_000)


def entry(entity_id: str, entity_type: CatalogEntityType, identifier: str, modes, lltv) -> CatalogEntry:
    return CatalogEntry(
        entity_id=entity_id,
        chain_id=1,
        entity_type=entity_type,
        address_or_market_id=identifier,
        name=entity_id,
        description=f"Synthetic {entity_id}",
        provenance=CatalogProvenance.REPRESENTATIVE_PROXY,
        provenance_label="synthetic",
        supported=True,
        supported_modes=modes,
        lltv=lltv,
    )


def market_entry() -> CatalogEntry:
    return entry(
        "weth-usdc-860",
        CatalogEntityType.MORPHO_MARKET,
        WETH_USDC_MARKET,
        (PositionMode.LENDER, PositionMode.BORROWER),
        Decimal("0.86"),
    )


def vault_entry() -> CatalogEntry:
    return entry(
        "mev-capital-weth",
        CatalogEntityType.MORPHO_VAULT_V1,
        VAULT_ADDRESS,
        (PositionMode.LENDER,),
        None,
    )


def market_snapshot(
    market_id: str = WETH_USDC_MARKET,
    *,
    loan_asset: AssetInfo | None = None,
    collateral_asset: AssetInfo | None = None,
    oracle_address: str = ORACLE_A,
    lltv: str = "0.86",
    **overrides,
) -> MarketSnapshot:
    values = {
        "chain_id": 1,
        "market_id": market_id,
        "loan_asset": loan_asset or usdc(),
        "collateral_asset": collateral_asset or weth(),
        "oracle_address": oracle_address,
        "irm_address": IRM,
        "lltv": Decimal(lltv),
        "total_supply_assets": Decimal("10000000.000000"),
        "total_borrow_assets": Decimal("8000000.000000"),
        "liquidity_assets": Decimal("2000000.000000"),
        "utilization": Decimal("0.8"),
        "total_supply_usd": Decimal("10000000"),
        "total_borrow_usd": Decimal("8000000"),
        "liquidity_usd": Decimal("2000000"),
        "listed": True,
        "last_accrual_timestamp": 1_791_000_000,
        "sources": (f"https://morpho.test/markets/{market_id}",),
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def vault_snapshot(**overrides) -> VaultSnapshot:
    values = {
        "chain_id": 1,
        "address": VAULT_ADDRESS,
        "version": "v1",
        "name": "MEV Capital wETH",
        "symbol": "MCwETH",
        "asset": weth(),
        "total_assets": Decimal("5000.000000000000000000"),
        "liquidity_assets": Decimal("1250.000000000000000000"),
        "idle_assets": Decimal("250.000000000000000000"),
        "share_price": Decimal("1.02"),
        "total_assets_usd": Decimal("10000000"),
        "liquidity_usd": Decimal("2500000"),
        "listed": True,
        "sources": ("https://morpho.test/vault",),
    }
    values.update(overrides)
    return VaultSnapshot(**values)


def allocation(market_id: str | None, weight: str, assets: str, *, adapter: str | None = None) -> AllocationSnapshot:
    return AllocationSnapshot(
        vault_address=VAULT_ADDRESS,
        market_id=market_id,
        adapter_address=adapter,
        supply_assets=Decimal(assets),
        supply_cap=Decimal("10000"),
        weight=Decimal(weight),
        pending_removal=False,
    )


def pool_snapshot(address: str = POOL_A, token0: AssetInfo | None = None, token1: AssetInfo | None = None) -> PoolSnapshot:
    return PoolSnapshot(
        chain_id=1,
        pool_address=address,
        token0=token0 or usdc(),
        token1=token1 or weth(),
        fee_tier=500,
        liquidity=Decimal("123456789012345678"),
        sqrt_price_x96=Decimal("1234567890123456789012345678"),
        sources=(f"https://uniswap.test/pools/{address}",),
    )


def oracle_observation(address: str = ORACLE_A, age: str | None = "60", deviation: str | None = "0.001") -> OracleObservation:
    return OracleObservation(oracle_address=address, age_seconds=_dec(age), deviation=_dec(deviation))


MARKET_SOURCE = "https://morpho.test/markets/weth-usdc/state"
VAULT_SOURCE = "https://morpho.test/vaults/mev/state"
ALLOCATIONS_SOURCE = "https://morpho.test/vaults/mev/allocations"
POOL_SOURCE = "https://uniswap.test/pool-a"
ORACLE_SOURCE = "https://rpc.test/oracle-a"


def borrower_position(market: MarketSnapshot | None = None, **overrides):
    values = {
        "mode": PositionMode.BORROWER,
        "entity_id": "weth-usdc-860",
        "amount": Decimal("3000"),
        "collateral_amount": Decimal("2.5"),
        "debt_amount": Decimal("3000"),
        "ltv": Decimal("0.60"),
        "numeraire": "USDC",
    }
    values.update(overrides)
    return validate_position(PositionInput(**values), market_entry(), market or market_snapshot(), now=VALIDATED_AT)


def market_lender_position(numeraire: str = "USDC"):
    position = PositionInput(mode=PositionMode.LENDER, entity_id="weth-usdc-860", amount=Decimal("1000"), numeraire=numeraire)
    return validate_position(position, market_entry(), market_snapshot(), now=VALIDATED_AT)


def vault_lender_position(numeraire: str = "WETH", amount: str = "10"):
    position = PositionInput(mode=PositionMode.LENDER, entity_id="mev-capital-weth", amount=Decimal(amount), numeraire=numeraire)
    return validate_position(position, vault_entry(), None, vault=vault_snapshot(), now=VALIDATED_AT)


def market_context(
    *,
    market: MarketSnapshot | None = None,
    with_pool: bool = True,
    with_oracle: bool = True,
    pool_depth: Decimal | None = Decimal("1000000"),
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
) -> GraphContext:
    pools = (envelope(pool_snapshot(), POOL_SOURCE),) if with_pool else ()
    oracles = {ORACLE_A: envelope(oracle_observation(), ORACLE_SOURCE)} if with_oracle else {}
    depths = {POOL_A: pool_depth} if pool_depth is not None else {}
    return GraphContext(
        entry=market_entry(),
        market=envelope(market or market_snapshot(), MARKET_SOURCE, freshness=freshness),
        exit_pools=pools,
        oracles=oracles,
        pool_depths=depths,
    )


def vault_context(
    *,
    allocations: tuple[AllocationSnapshot, ...] | None = (
        allocation(WETH_USDC_MARKET, "0.4", "2000"),
        allocation(WBTC_USDC_MARKET, "0.6", "3000"),
        allocation(EMPTY_MARKET, "0", "0"),
    ),
    expand_markets: bool = True,
    with_pool: bool = True,
) -> GraphContext:
    markets = {}
    if expand_markets:
        markets = {
            WETH_USDC_MARKET: envelope(market_snapshot(loan_asset=weth(), collateral_asset=usdc(), oracle_address=ORACLE_A), MARKET_SOURCE),
            WBTC_USDC_MARKET: envelope(
                market_snapshot(WBTC_USDC_MARKET, loan_asset=weth(), collateral_asset=wbtc(), oracle_address=ORACLE_B),
                "https://morpho.test/markets/wbtc/state",
            ),
        }
    return GraphContext(
        entry=vault_entry(),
        vault=envelope(vault_snapshot(), VAULT_SOURCE),
        allocations=None if allocations is None else envelope(allocations, ALLOCATIONS_SOURCE),
        exit_pools=(envelope(pool_snapshot(), POOL_SOURCE),) if with_pool else (),
        allocation_markets=markets,
        oracles={ORACLE_A: envelope(oracle_observation(ORACLE_A), ORACLE_SOURCE)},
        pool_depths={POOL_A: Decimal("500")},
    )


def build(position, context) -> RiskGraph:
    return build_risk_graph(position, context, clock=lambda: BUILT_AT)


def nodes_by_id(graph: RiskGraph):
    return {node.node_id: node for node in graph.nodes}


def edge_set(graph: RiskGraph):
    return {(edge.source, edge.target, edge.relationship, edge.exposure_weight) for edge in graph.edges}


def all_selections() -> tuple[ScenarioSelection, ...]:
    return tuple(
        ScenarioSelection(scenario_id=scenario_id, severity=SCENARIO_DEFINITIONS[scenario_id].default_severity)
        for scenario_id in ScenarioId
    )


# Node identifiers


def test_node_id_format_is_type_chain_key_in_lowercase():
    assert node_id(RiskNodeType.ASSET, WETH_ADDRESS) == WETH_ID
    assert node_id(RiskNodeType.MARKET, WETH_USDC_MARKET.upper().replace("0X", "0x")) == MARKET_A_ID
    assert node_id(RiskNodeType.PROTOCOL, "morpho") == MORPHO_ID


def test_node_id_rejects_empty_keys():
    with pytest.raises(ValueError):
        node_id(RiskNodeType.ASSET, "")


# Market graphs


def test_borrower_market_graph_has_the_expected_nodes_and_edges():
    graph = build(borrower_position(), market_context())

    nodes = nodes_by_id(graph)
    assert set(nodes) == {POSITION_BORROWER, MARKET_A_ID, USDC_ID, WETH_ID, ORACLE_A_ID, MORPHO_ID, ETHEREUM_ID, POOL_A_ID}
    assert nodes[POSITION_BORROWER].node_type is RiskNodeType.POSITION
    assert nodes[MARKET_A_ID].node_type is RiskNodeType.MARKET
    assert nodes[USDC_ID].node_type is RiskNodeType.ASSET
    assert nodes[ORACLE_A_ID].node_type is RiskNodeType.ORACLE
    assert nodes[MORPHO_ID].node_type is RiskNodeType.PROTOCOL
    assert nodes[ETHEREUM_ID].node_type is RiskNodeType.CHAIN
    assert nodes[POOL_A_ID].node_type is RiskNodeType.POOL

    assert edge_set(graph) == {
        (POSITION_BORROWER, MARKET_A_ID, RiskEdgeType.BORROWS, None),
        (POSITION_BORROWER, USDC_ID, RiskEdgeType.BORROWS, None),
        (POSITION_BORROWER, WETH_ID, RiskEdgeType.USES_COLLATERAL, None),
        (POSITION_BORROWER, POOL_A_ID, RiskEdgeType.EXITS_THROUGH, None),
        (MARKET_A_ID, USDC_ID, RiskEdgeType.LENDS, None),
        (MARKET_A_ID, WETH_ID, RiskEdgeType.USES_COLLATERAL, None),
        (MARKET_A_ID, ORACLE_A_ID, RiskEdgeType.USES_ORACLE, None),
        (MARKET_A_ID, MORPHO_ID, RiskEdgeType.DEPLOYED_ON, None),
        (MORPHO_ID, ETHEREUM_ID, RiskEdgeType.DEPLOYED_ON, None),
        (POOL_A_ID, USDC_ID, RiskEdgeType.EXITS_THROUGH, None),
        (POOL_A_ID, WETH_ID, RiskEdgeType.EXITS_THROUGH, None),
    }
    assert graph.generated_at == BUILT_AT
    assert graph.completeness_warnings == ()


def test_lender_market_graph_lends_instead_of_borrowing():
    graph = build(market_lender_position(), market_context())

    edges = edge_set(graph)
    assert (POSITION_LENDER, MARKET_A_ID, RiskEdgeType.LENDS, None) in edges
    assert not any(edge[2] in (RiskEdgeType.BORROWS,) for edge in edges)
    assert not any(edge[0] == POSITION_LENDER and edge[2] is RiskEdgeType.USES_COLLATERAL for edge in edges)
    assert (MARKET_A_ID, WETH_ID, RiskEdgeType.USES_COLLATERAL, None) in edges


def test_market_graph_metrics_match_engine_keys():
    graph = build(borrower_position(), market_context())
    nodes = nodes_by_id(graph)

    assert nodes[MARKET_A_ID].metrics["lltv"] == Decimal("0.86")
    assert nodes[MARKET_A_ID].metrics["available_liquidity"] == Decimal("2000000")
    assert nodes[MARKET_A_ID].metrics["loan_symbol"] == "USDC"
    assert nodes[MARKET_A_ID].metrics["collateral_symbol"] == "WETH"
    assert nodes[USDC_ID].metrics["symbol"] == "USDC"
    assert nodes[WETH_ID].metrics["symbol"] == "WETH"
    assert nodes[WETH_ID].metrics["price_usd"] == Decimal("2000")
    assert nodes[WETH_ID].metrics["decimals"] == Decimal("18")
    assert nodes[ORACLE_A_ID].metrics["age_seconds"] == Decimal("60")
    assert nodes[ORACLE_A_ID].metrics["deviation"] == Decimal("0.001")
    assert nodes[POOL_A_ID].metrics["liquidity_depth"] == Decimal("1000000")
    assert nodes[POOL_A_ID].metrics["fee_tier"] == Decimal("500")
    for node in graph.nodes:
        for value in node.metrics.values():
            assert not isinstance(value, float)


def test_position_node_carries_validated_values_and_user_provenance():
    position = borrower_position()
    graph = build(position, market_context())
    node = nodes_by_id(graph)[POSITION_BORROWER]

    assert node.provenance == POSITION_PROVENANCE
    assert node.freshness is FreshnessStatus.FRESH
    assert node.observed_at == VALIDATED_AT
    assert node.metrics["mode"] == "borrower"
    assert node.metrics["entity_id"] == "weth-usdc-860"
    assert node.metrics["numeraire"] == "USDC"
    assert node.metrics["amount"] == Decimal("3000")
    assert node.metrics["collateral_amount"] == Decimal("2.5")
    assert node.metrics["debt_amount"] == Decimal("3000")
    assert node.metrics["ltv"] == Decimal("0.60")
    assert node.metrics["collateral_value"] == Decimal("5000")
    assert node.metrics["debt_value"] == Decimal("3000")
    assert node.metrics["lltv"] == Decimal("0.86")


def test_available_liquidity_is_expressed_in_the_position_numeraire():
    usd_graph = build(borrower_position(numeraire="USD"), market_context())
    weth_graph = build(borrower_position(numeraire="WETH", amount=Decimal("1.5")), market_context())

    assert nodes_by_id(usd_graph)[MARKET_A_ID].metrics["available_liquidity"] == Decimal("2000000")
    assert nodes_by_id(weth_graph)[MARKET_A_ID].metrics["available_liquidity"] == Decimal("1000")


def test_available_liquidity_is_none_with_warning_when_not_convertible():
    market = market_snapshot(loan_asset=usdc(price=None), liquidity_usd=None)
    position = borrower_position(market, numeraire="USD")
    context = market_context(market=market)

    graph = build(position, context)

    assert nodes_by_id(graph)[MARKET_A_ID].metrics["available_liquidity"] is None
    assert any("liquidity" in warning.lower() for warning in graph.completeness_warnings)


# Vault graphs


def test_vault_graph_has_one_allocated_to_edge_per_non_zero_allocation():
    graph = build(vault_lender_position(), vault_context())
    edges = edge_set(graph)

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.4")) in edges
    assert (VAULT_ID, MARKET_B_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.6")) in edges
    allocated = [edge for edge in graph.edges if edge.relationship is RiskEdgeType.ALLOCATED_TO]
    assert len(allocated) == 2
    assert f"market:1:{EMPTY_MARKET}" not in nodes_by_id(graph)
    assert all(isinstance(edge.exposure_weight, Decimal) for edge in allocated)


def test_vault_graph_structure_matches_the_engine_fixture_shape():
    graph = build(vault_lender_position(), vault_context())
    edges = edge_set(graph)
    nodes = nodes_by_id(graph)

    assert {POSITION_VAULT, VAULT_ID, MARKET_A_ID, MARKET_B_ID, WETH_ID, USDC_ID, WBTC_ID, ORACLE_A_ID, ORACLE_B_ID, MORPHO_ID, ETHEREUM_ID, POOL_A_ID} == set(nodes)
    assert (POSITION_VAULT, VAULT_ID, RiskEdgeType.LENDS, None) in edges
    assert (VAULT_ID, WETH_ID, RiskEdgeType.LENDS, None) in edges
    assert (VAULT_ID, MORPHO_ID, RiskEdgeType.DEPLOYED_ON, None) in edges
    assert (MARKET_A_ID, WETH_ID, RiskEdgeType.LENDS, None) in edges
    assert (MARKET_A_ID, USDC_ID, RiskEdgeType.USES_COLLATERAL, None) in edges
    assert (MARKET_A_ID, ORACLE_A_ID, RiskEdgeType.USES_ORACLE, None) in edges
    assert (MARKET_B_ID, WBTC_ID, RiskEdgeType.USES_COLLATERAL, None) in edges
    assert (MARKET_B_ID, ORACLE_B_ID, RiskEdgeType.USES_ORACLE, None) in edges
    assert (MARKET_B_ID, MORPHO_ID, RiskEdgeType.DEPLOYED_ON, None) in edges
    assert (MORPHO_ID, ETHEREUM_ID, RiskEdgeType.DEPLOYED_ON, None) in edges
    assert (POSITION_VAULT, POOL_A_ID, RiskEdgeType.EXITS_THROUGH, None) in edges
    assert nodes[VAULT_ID].metrics["symbol"] == "MCwETH"
    assert nodes[VAULT_ID].metrics["available_liquidity"] == Decimal("1250")
    assert nodes[MARKET_A_ID].metrics["allocation_weight"] == Decimal("0.4")
    assert nodes[MARKET_A_ID].metrics["allocated_assets"] == Decimal("2000")
    assert nodes[MARKET_B_ID].metrics["lltv"] == Decimal("0.86")


def test_vault_available_liquidity_in_usd_uses_the_envelope_usd_value():
    graph = build(vault_lender_position(numeraire="USD", amount="20000"), vault_context())

    assert nodes_by_id(graph)[VAULT_ID].metrics["available_liquidity"] == Decimal("2500000")


def test_allocated_market_without_details_is_kept_with_a_warning():
    graph = build(vault_lender_position(), vault_context(expand_markets=False))
    nodes = nodes_by_id(graph)

    assert MARKET_A_ID in nodes and MARKET_B_ID in nodes
    assert nodes[MARKET_A_ID].metrics.get("lltv") is None
    assert nodes[MARKET_A_ID].provenance == ALLOCATIONS_SOURCE
    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.4")) in edge_set(graph)
    assert any(WETH_USDC_MARKET in warning for warning in graph.completeness_warnings)


def test_missing_allocations_produce_a_warning_not_fabricated_edges():
    graph = build(vault_lender_position(), vault_context(allocations=None))

    assert not any(edge.relationship is RiskEdgeType.ALLOCATED_TO for edge in graph.edges)
    assert any("allocation" in warning.lower() for warning in graph.completeness_warnings)


def test_adapter_level_allocation_without_market_is_a_visible_unmodelled_node():
    adapter = "0x00000000000000000000000000000000000000ad"
    context = vault_context(
        allocations=(allocation(None, "0.7", "3500", adapter=adapter), allocation(WETH_USDC_MARKET, "0.3", "1500"))
    )

    graph = build(vault_lender_position(), context)
    nodes = nodes_by_id(graph)
    unmodelled_id = f"protocol:1:{adapter}"

    # The unknown 70% is a node and a weighted edge, never silently dropped (spec section 15).
    assert unmodelled_id in nodes
    node = nodes[unmodelled_id]
    assert node.node_type is RiskNodeType.PROTOCOL
    assert node.label == "Unmodelled allocation"
    assert node.metrics["exposure_unknown"] is True
    assert node.metrics["weight"] == Decimal("0.7")
    assert node.metrics["allocated_assets"] == Decimal("3500")
    assert node.metrics["adapter_address"] == adapter
    assert node.provenance == ALLOCATIONS_SOURCE
    edges = edge_set(graph)
    assert (VAULT_ID, unmodelled_id, RiskEdgeType.ALLOCATED_TO, Decimal("0.7")) in edges
    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.3")) in edges
    allocated = [edge for edge in graph.edges if edge.relationship is RiskEdgeType.ALLOCATED_TO]
    assert len(allocated) == 2
    assert any(adapter in warning for warning in graph.completeness_warnings)
    assert any(
        "70.00% of vault allocations could not be modelled" in warning
        and "scenario exposure for that share is unknown" in warning
        for warning in graph.completeness_warnings
    )
    # The engine still accepts the graph; treating exposure_unknown as UNAVAILABLE is its follow-up.
    results = ScenarioEngine().run(vault_lender_position(), graph, all_selections(), {})
    assert len(results) == len(ScenarioId)


def test_unmodelled_allocation_without_adapter_address_gets_an_indexed_node():
    context = vault_context(
        allocations=(allocation(WETH_USDC_MARKET, "0.75", "3750"), allocation(None, "0.25", "1250"))
    )

    graph = build(vault_lender_position(), context)
    nodes = nodes_by_id(graph)

    unmodelled_id = "protocol:1:unmodelled-allocation-1"
    assert unmodelled_id in nodes
    assert nodes[unmodelled_id].metrics["exposure_unknown"] is True
    assert nodes[unmodelled_id].metrics["adapter_address"] is None
    assert (VAULT_ID, unmodelled_id, RiskEdgeType.ALLOCATED_TO, Decimal("0.25")) in edge_set(graph)
    assert any("25.00% of vault allocations could not be modelled" in warning for warning in graph.completeness_warnings)
    assert all(NODE_ID_PATTERN.match(node.node_id) for node in graph.nodes)


def test_allocation_rows_to_the_same_market_are_summed():
    adapter_a = "0x" + "a" * 40
    adapter_b = "0x" + "b" * 40
    context = vault_context(
        allocations=(
            allocation(WETH_USDC_MARKET, "0.3", "1500", adapter=adapter_a),
            allocation(WETH_USDC_MARKET, "0.3", "1500", adapter=adapter_b),
            allocation(WBTC_USDC_MARKET, "0.4", "2000"),
        )
    )

    graph = build(vault_lender_position(), context)
    nodes = nodes_by_id(graph)

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.6")) in edge_set(graph)
    assert nodes[MARKET_A_ID].metrics["allocation_weight"] == Decimal("0.6")
    assert nodes[MARKET_A_ID].metrics["allocated_assets"] == Decimal("3000")
    assert nodes[MARKET_A_ID].metrics["adapter_address"] is None
    assert nodes[MARKET_B_ID].metrics["adapter_address"] is None
    assert not any("exceed" in warning for warning in graph.completeness_warnings)


def test_summed_allocation_rows_above_one_are_capped_with_a_warning():
    context = vault_context(
        allocations=(
            allocation(WETH_USDC_MARKET, "0.6", "3000", adapter="0x" + "a" * 40),
            allocation("0x" + WETH_USDC_MARKET[2:].upper(), "0.6", "3000", adapter="0x" + "b" * 40),
        )
    )

    graph = build(vault_lender_position(), context)

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("1")) in edge_set(graph)
    assert any("capped" in warning for warning in graph.completeness_warnings)


def test_negative_allocation_weight_is_skipped_with_a_warning():
    context = vault_context(
        allocations=(allocation(WETH_USDC_MARKET, "0.7", "3500"), allocation(WBTC_USDC_MARKET, "-0.2", "1000"))
    )

    graph = build(vault_lender_position(), context)

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.7")) in edge_set(graph)
    assert MARKET_B_ID not in nodes_by_id(graph)
    assert any("-0.2" in warning and "negative" in warning.lower() for warning in graph.completeness_warnings)


def test_allocation_weights_summing_above_one_produce_a_warning():
    context = vault_context(
        allocations=(allocation(WETH_USDC_MARKET, "0.7", "3500"), allocation(WBTC_USDC_MARKET, "0.6", "3000"))
    )

    graph = build(vault_lender_position(), context)
    edges = edge_set(graph)

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.7")) in edges
    assert (VAULT_ID, MARKET_B_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.6")) in edges
    assert any("1.3" in warning and "exceed" in warning.lower() for warning in graph.completeness_warnings)


def test_allocation_for_another_vault_is_rejected():
    foreign = AllocationSnapshot(
        vault_address="0x" + "9" * 40,
        market_id=WETH_USDC_MARKET,
        adapter_address=None,
        supply_assets=Decimal("1"),
        supply_cap=None,
        weight=Decimal("0.5"),
        pending_removal=False,
    )

    with pytest.raises(GraphConstructionError, match="vault"):
        build(vault_lender_position(), vault_context(allocations=(foreign,)))


def test_allocation_vault_address_comparison_ignores_hex_case():
    row = AllocationSnapshot(
        vault_address="0x" + VAULT_ADDRESS[2:].upper(),
        market_id=WETH_USDC_MARKET,
        adapter_address=None,
        supply_assets=Decimal("2000"),
        supply_cap=None,
        weight=Decimal("0.4"),
        pending_removal=False,
    )

    graph = build(vault_lender_position(), vault_context(allocations=(row,)))

    assert (VAULT_ID, MARKET_A_ID, RiskEdgeType.ALLOCATED_TO, Decimal("0.4")) in edge_set(graph)


# Provenance and freshness


def test_every_node_carries_provenance_freshness_and_observed_at_from_its_envelope():
    stale_fetch = FETCHED_AT - timedelta(minutes=10)
    context = market_context(freshness=FreshnessStatus.STALE_USABLE)
    context = GraphContext(
        entry=context.entry,
        market=envelope(market_snapshot(), MARKET_SOURCE, freshness=FreshnessStatus.STALE_USABLE, fetched_at=stale_fetch),
        exit_pools=context.exit_pools,
        oracles=context.oracles,
        pool_depths=context.pool_depths,
    )

    graph = build(borrower_position(), context)
    nodes = nodes_by_id(graph)

    for node_key in (MARKET_A_ID, USDC_ID, WETH_ID):
        assert nodes[node_key].provenance == MARKET_SOURCE
        assert nodes[node_key].freshness is FreshnessStatus.STALE_USABLE
        assert nodes[node_key].observed_at == stale_fetch
    assert nodes[ORACLE_A_ID].provenance == ORACLE_SOURCE
    assert nodes[ORACLE_A_ID].observed_at == FETCHED_AT
    assert nodes[POOL_A_ID].provenance == POOL_SOURCE
    assert nodes[POOL_A_ID].freshness is FreshnessStatus.FRESH
    assert nodes[MORPHO_ID].provenance == CATALOG_PROVENANCE
    assert nodes[ETHEREUM_ID].provenance == CATALOG_PROVENANCE
    assert nodes[MORPHO_ID].observed_at == BUILT_AT
    for node in graph.nodes:
        assert node.provenance
        assert node.observed_at.tzinfo is not None


def test_oracle_observation_must_match_its_mapping_key():
    context = GraphContext(
        entry=market_entry(),
        market=envelope(market_snapshot(), MARKET_SOURCE),
        oracles={ORACLE_A: envelope(oracle_observation(ORACLE_B, age="99999", deviation="0.5"), ORACLE_SOURCE)},
    )

    with pytest.raises(GraphConstructionError, match="oracle"):
        build(borrower_position(), context)


def test_oracle_observation_key_comparison_ignores_hex_case():
    context = GraphContext(
        entry=market_entry(),
        market=envelope(market_snapshot(), MARKET_SOURCE),
        oracles={"0x" + ORACLE_A[2:].upper(): envelope(oracle_observation(ORACLE_A), ORACLE_SOURCE)},
    )

    graph = build(borrower_position(), context)

    assert nodes_by_id(graph)[ORACLE_A_ID].metrics["age_seconds"] == Decimal("60")


def test_envelope_source_credentials_and_query_are_stripped_from_provenance():
    secret_source = "https://user:secret@rpc.example/v1/abc?key=x#frag"
    context = GraphContext(
        entry=market_entry(),
        market=envelope(market_snapshot(), "morpho graphql fixture"),
        exit_pools=(envelope(pool_snapshot(), secret_source),),
        oracles={ORACLE_A: envelope(oracle_observation(), "https://rpc.example:8545/oracle?token=abc")},
        pool_depths={POOL_A: Decimal("1000000")},
    )

    graph = build(borrower_position(), context)
    nodes = nodes_by_id(graph)

    assert nodes[POOL_A_ID].provenance == "https://rpc.example/v1/abc"
    assert nodes[ORACLE_A_ID].provenance == "https://rpc.example:8545/oracle"
    assert nodes[MARKET_A_ID].provenance == "morpho graphql fixture"
    for node in graph.nodes:
        assert "secret" not in node.provenance
        assert "key=x" not in node.provenance
        assert "token=abc" not in node.provenance


def test_oracle_without_live_observation_has_none_metrics_and_a_warning():
    graph = build(borrower_position(), market_context(with_oracle=False))
    node = nodes_by_id(graph)[ORACLE_A_ID]

    assert node.metrics["age_seconds"] is None
    assert node.metrics["deviation"] is None
    assert node.provenance == MARKET_SOURCE
    assert any("oracle" in warning.lower() for warning in graph.completeness_warnings)


# Completeness warnings


def test_missing_exit_pools_produce_a_warning():
    graph = build(borrower_position(), market_context(with_pool=False))

    assert not any(node.node_type is RiskNodeType.POOL for node in graph.nodes)
    assert any("exit pool" in warning.lower() for warning in graph.completeness_warnings)


def test_exit_pool_on_another_chain_is_rejected():
    foreign_pool = pool_snapshot().model_copy(update={"chain_id": 10})
    context = GraphContext(
        entry=market_entry(),
        market=envelope(market_snapshot(), MARKET_SOURCE),
        exit_pools=(envelope(foreign_pool, POOL_SOURCE),),
        pool_depths={POOL_A: Decimal("1000000")},
    )

    with pytest.raises(GraphConstructionError, match="chain"):
        build(borrower_position(), context)


def test_exit_pool_unrelated_to_the_graph_assets_is_omitted_with_a_warning():
    dai = AssetInfo(address="0x6B175474E89094C44Da98b954EedeAC495271d0F", symbol="DAI", decimals=18, price_usd=Decimal(1))
    unrelated = pool_snapshot(POOL_B, token0=dai, token1=wbtc())
    context = GraphContext(
        entry=market_entry(),
        market=envelope(market_snapshot(), MARKET_SOURCE),
        exit_pools=(envelope(pool_snapshot(), POOL_SOURCE), envelope(unrelated, POOL_SOURCE)),
        pool_depths={POOL_A: Decimal("1000000"), POOL_B: Decimal("5000000")},
    )

    graph = build(borrower_position(), context)
    nodes = nodes_by_id(graph)

    assert POOL_A_ID in nodes
    assert POOL_B_ID not in nodes
    assert not any(POOL_B_ID in (edge.source, edge.target) for edge in graph.edges)
    assert any(POOL_B in warning and "DAI/WBTC" in warning for warning in graph.completeness_warnings)
    liquidity = next(
        result
        for result in ScenarioEngine().run(borrower_position(), graph, all_selections(), {})
        if result.scenario_id == ScenarioId.LIQUIDITY_COLLAPSE.value
    )
    assert liquidity.stressed_values["exit_pool_depth"] == Decimal("1000000")


def test_pool_without_depth_has_none_depth_and_a_warning():
    graph = build(borrower_position(), market_context(pool_depth=None))

    assert nodes_by_id(graph)[POOL_A_ID].metrics["liquidity_depth"] is None
    assert any("depth" in warning.lower() for warning in graph.completeness_warnings)


def test_position_validation_warnings_are_carried_into_the_graph():
    market = market_snapshot(collateral_asset=weth(price=None))
    position = borrower_position(market)
    graph = build(position, market_context(market=market))

    assert position.warnings
    for warning in position.warnings:
        assert warning in graph.completeness_warnings
    assert nodes_by_id(graph)[WETH_ID].metrics["price_usd"] is None


def test_completeness_warnings_are_deterministic_and_unique():
    context = market_context(with_pool=False, with_oracle=False)

    first = build(borrower_position(), context)
    second = build(borrower_position(), context)

    assert first.completeness_warnings == second.completeness_warnings
    assert len(set(first.completeness_warnings)) == len(first.completeness_warnings)


# Determinism and guards


def test_graph_is_deterministic_and_ids_are_well_formed():
    first = build(vault_lender_position(), vault_context())
    second = build(vault_lender_position(), vault_context())

    assert first == second
    assert first.nodes[0].node_type is RiskNodeType.POSITION
    assert len({node.node_id for node in first.nodes}) == len(first.nodes)
    assert len(set(first.edges)) == len(first.edges)
    for node in first.nodes:
        assert NODE_ID_PATTERN.match(node.node_id), node.node_id
    for edge in first.edges:
        assert edge.source != edge.target


def test_unsupported_mode_is_rejected_before_construction():
    position = borrower_position()
    context = GraphContext(entry=entry("weth-usdc-860", CatalogEntityType.MORPHO_MARKET, WETH_USDC_MARKET, (PositionMode.LENDER,), Decimal("0.86")), market=envelope(market_snapshot(), MARKET_SOURCE))

    with pytest.raises(GraphConstructionError, match="mode"):
        build(position, context)


def test_context_entry_must_match_the_position():
    with pytest.raises(GraphConstructionError, match="entity"):
        build(borrower_position(), vault_context())


def test_market_entry_requires_a_market_envelope():
    with pytest.raises(GraphConstructionError, match="market"):
        build(borrower_position(), GraphContext(entry=market_entry()))


def test_vault_entry_requires_a_vault_envelope():
    with pytest.raises(GraphConstructionError, match="vault"):
        build(vault_lender_position(), GraphContext(entry=vault_entry()))


def test_market_envelope_must_match_the_entry():
    context = GraphContext(entry=market_entry(), market=envelope(market_snapshot(WBTC_USDC_MARKET), MARKET_SOURCE))

    with pytest.raises(GraphConstructionError, match="market"):
        build(borrower_position(), context)


# Integration with Task 6's scenario engine


def test_scenario_engine_accepts_a_built_borrower_graph():
    position = borrower_position()
    graph = build(position, market_context())

    results = ScenarioEngine().run(position, graph, all_selections(), {})

    assert len(results) == len(ScenarioId)
    by_id = {result.scenario_id: result for result in results}
    shock = by_id[ScenarioId.COLLATERAL_SHOCK.value]
    assert shock.stressed_values["stressed_ltv"] == position.debt_value / (position.collateral_value * Decimal("0.85"))
    assert shock.stressed_values["lltv"] == Decimal("0.86")
    assert shock.affected_path == (POSITION_BORROWER, MARKET_A_ID, WETH_ID)
    assert by_id[ScenarioId.LIQUIDITY_COLLAPSE.value].stressed_values["stressed_exit_pool_depth"] == Decimal("200000.00")
    assert by_id[ScenarioId.ORACLE_FAILURE.value].stressed_values["oracle_max_age_seconds"] == Decimal("60")


def test_scenario_engine_accepts_a_built_vault_graph():
    position = vault_lender_position()
    graph = build(position, vault_context())

    results = ScenarioEngine().run(position, graph, all_selections(), {})

    assert len(results) == len(ScenarioId)
    by_id = {result.scenario_id: result for result in results}
    # WETH is the vault asset, not collateral, so a collateral shock only hits WBTC via market B.
    shock = by_id[ScenarioId.COLLATERAL_SHOCK.value]
    assert shock.impact == Decimal("10") * Decimal("0.6") * Decimal("0.15")
    assert by_id[ScenarioId.PROTOCOL_IMPAIRMENT.value].impact == Decimal("1.0")
    liquidity = by_id[ScenarioId.LIQUIDITY_COLLAPSE.value]
    assert liquidity.stressed_values["available_liquidity"] == Decimal("1250")


def test_scenario_engine_reports_unavailable_when_graph_data_is_missing():
    market = market_snapshot(collateral_asset=weth(price=None))
    position = borrower_position(market)
    graph = build(position, market_context(market=market, with_pool=False, with_oracle=False))

    results = ScenarioEngine().run(position, graph, all_selections(), {})

    shock = next(result for result in results if result.scenario_id == ScenarioId.COLLATERAL_SHOCK.value)
    assert shock.confidence is ConfidenceLevel.UNAVAILABLE
    assert shock.stressed_values["stressed_ltv"] is None
