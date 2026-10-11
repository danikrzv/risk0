"""Solvency annotations on the risk graph: node metrics, provenance and edge loss shares."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

from app.analysis.solvency_graph import (
    CollateralExit,
    MarketSolvencyInputs,
    Observed,
    annotate_solvency,
)
from app.contracts.models import (
    FreshnessStatus,
    MetricProvenance,
    MetricProvenanceKind,
    PositionMode,
    RiskEdge,
    RiskEdgeType,
    RiskGraph,
    RiskNode,
    RiskNodeType,
)
from app.domain.solvency import R_PANIC, LiquidationZone


D = Decimal
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
LLTV = D("0.86")
POSITION = "position:1:lender-test"
VAULT = "vault:1:0x00000000000000000000000000000000000000a1"
USDC = "asset:1:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "asset:1:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
MORPHO_API = "https://api.morpho.org/graphql"


def market_id(letter: str) -> str:
    return "0x" + letter * 64


def market_node_id(letter: str) -> str:
    return f"market:1:{market_id(letter)}"


def oracle_address(letter: str) -> str:
    return "0x" + letter * 40


def oracle_node_id(letter: str) -> str:
    return f"oracle:1:{oracle_address(letter)}"


def node(node_id: str, node_type: RiskNodeType, metrics: dict) -> RiskNode:
    return RiskNode(
        node_id=node_id,
        node_type=node_type,
        label=node_id,
        metrics=metrics,
        provenance=MORPHO_API,
        freshness=FreshnessStatus.FRESH,
        observed_at=NOW,
    )


def market_nodes(letter: str, *, total_supply: Decimal, allocated: Decimal | None = None):
    metrics = {
        "market_id": market_id(letter),
        "lltv": LLTV,
        "oracle_address": oracle_address(letter),
        "total_supply_assets": total_supply,
        "total_borrow_assets": total_supply * D("0.8"),
    }
    if allocated is not None:
        metrics["allocated_assets"] = allocated
    nodes = [
        node(market_node_id(letter), RiskNodeType.MARKET, metrics),
        node(oracle_node_id(letter), RiskNodeType.ORACLE, {"address": oracle_address(letter)}),
    ]
    edges = [
        RiskEdge(source=market_node_id(letter), target=USDC, relationship=RiskEdgeType.LENDS),
        RiskEdge(source=market_node_id(letter), target=WETH, relationship=RiskEdgeType.USES_COLLATERAL),
        RiskEdge(source=market_node_id(letter), target=oracle_node_id(letter), relationship=RiskEdgeType.USES_ORACLE),
    ]
    return nodes, edges


def assets() -> list[RiskNode]:
    return [
        node(USDC, RiskNodeType.ASSET, {"symbol": "USDC"}),
        node(WETH, RiskNodeType.ASSET, {"symbol": "WETH"}),
    ]


def position_node(mode: PositionMode = PositionMode.LENDER, amount: Decimal = D("10000")) -> RiskNode:
    return node(POSITION, RiskNodeType.POSITION, {"mode": mode.value, "amount": amount, "numeraire": "USDC"})


def market_graph(mode: PositionMode = PositionMode.LENDER) -> RiskGraph:
    nodes, edges = market_nodes("a", total_supply=D("1000000"))
    relationship = RiskEdgeType.LENDS if mode is PositionMode.LENDER else RiskEdgeType.BORROWS
    return RiskGraph(
        nodes=(position_node(mode), *nodes, *assets()),
        edges=(RiskEdge(source=POSITION, target=market_node_id("a"), relationship=relationship), *edges),
        generated_at=NOW,
    )


def vault_graph(*, unmodelled: bool = False) -> RiskGraph:
    a_nodes, a_edges = market_nodes("a", total_supply=D("500"), allocated=D("100000"))
    b_nodes, b_edges = market_nodes("b", total_supply=D("1000000"), allocated=D("200000"))
    vault = node(VAULT, RiskNodeType.VAULT, {"total_assets": D("300000"), "asset_symbol": "USDC"})
    nodes = [position_node(amount=D("3000")), vault, *a_nodes, *b_nodes, *assets()]
    edges = [
        RiskEdge(source=POSITION, target=VAULT, relationship=RiskEdgeType.LENDS),
        RiskEdge(source=VAULT, target=market_node_id("a"), relationship=RiskEdgeType.ALLOCATED_TO, exposure_weight=D("0.333")),
        RiskEdge(source=VAULT, target=market_node_id("b"), relationship=RiskEdgeType.ALLOCATED_TO, exposure_weight=D("0.667")),
        *a_edges,
        *b_edges,
    ]
    if unmodelled:
        hidden = "protocol:1:0x00000000000000000000000000000000000000f9"
        nodes.append(node(hidden, RiskNodeType.PROTOCOL, {"exposure_unknown": True}))
        edges.append(RiskEdge(source=VAULT, target=hidden, relationship=RiskEdgeType.ALLOCATED_TO, exposure_weight=D("0.1")))
    return RiskGraph(nodes=tuple(nodes), edges=tuple(edges), generated_at=NOW)


def data(source: str, freshness: FreshnessStatus = FreshnessStatus.FRESH, block: int | None = 21_000_000):
    return MetricProvenance(
        kind=MetricProvenanceKind.DATA, source=source, fetched_at=NOW, freshness=freshness, block_number=block
    )


def inputs(
    letter: str,
    *,
    q: Decimal | None,
    r: Decimal | None = D("1000"),
    p_contract: Decimal | None = D("2000"),
    spot: Decimal | None = D("1990"),
    bound: bool = False,
    oracle_freshness: FreshnessStatus = FreshnessStatus.FRESH,
) -> MarketSolvencyInputs:
    zone = None if q is None else LiquidationZone(collateral=q, borrowers_in_zone=3, uncovered_debt=D(0), bound=bound)
    exit_ = None
    if r is not None:
        exit_ = CollateralExit(
            key=f"collateral-exit-{letter}",
            label="WETH -> USDC (Uniswap v3)",
            route=("WETH", "USDC"),
            depth=Observed(r, data("uniswap:onchain")),
            spot_price=None if spot is None else Observed(spot, data("uniswap:onchain")),
            token_ids=(WETH, USDC),
        )
    return MarketSolvencyInputs(
        market_id=market_id(letter),
        zone=zone,
        zone_provenance=data(MORPHO_API, block=None),
        p_contract=None if p_contract is None else Observed(p_contract, data("chainlink:oracle", oracle_freshness)),
        exit=exit_,
    )


def by_id(graph: RiskGraph) -> dict[str, RiskNode]:
    return {item.node_id: item for item in graph.nodes}


def edge(graph: RiskGraph, source: str, target: str) -> RiskEdge:
    return next(item for item in graph.edges if item.source == source and item.target == target)


def exact(compute):
    with localcontext() as context:
        context.prec = 80
        return compute()


# Market lender


def test_scenario_b_market_metrics_provenance_and_edges() -> None:
    graph = annotate_solvency(
        market_graph(), {market_id("a"): inputs("a", q=D("40"), r=D("100"), spot=D("1800"))},
        x_in_pool_units=D("10000"),
    )
    nodes = by_id(graph)
    market = nodes[market_node_id("a")].metrics
    assert market["solvency_scenario"] == "B_degraded"
    assert market["solvency_q"] == D("40")
    assert market["solvency_q_over_half_r"] == D("0.8")
    assert market["solvency_debt_issued"] == D("68800")
    recovered = exact(lambda: D("40") * D("1800") * (D("100") / D("140")) ** 2)
    assert abs(market["solvency_recovered_value"] - recovered) < D("1e-17")
    assert market["solvency_bad_debt"] > 0
    provenance = nodes[market_node_id("a")].metric_provenance
    assert provenance["solvency_q"].kind is MetricProvenanceKind.DATA
    assert provenance["solvency_q"].source == MORPHO_API
    assert provenance["solvency_bad_debt"].kind is MetricProvenanceKind.DERIVED

    oracle = nodes[oracle_node_id("a")]
    assert oracle.metrics["solvency_p_contract"] == D("2000")
    assert oracle.metrics["solvency_p_real"] == D("1800")
    assert oracle.metrics["solvency_price_sources"] == "morpho_oracle,uniswap_spot"
    assert oracle.metrics["solvency_price_deviation"] == D("0.1")
    assert oracle.metric_provenance["solvency_p_contract"].source == "chainlink:oracle"

    pool_id = "pool:1:collateral-exit-a"
    assert nodes[pool_id].metrics["solvency_r"] == D("100")
    assert nodes[pool_id].metric_provenance["solvency_r"].block_number == 21_000_000
    assert edge(graph, market_node_id("a"), pool_id).loss_share == D(1)
    assert edge(graph, market_node_id("a"), pool_id).relationship is RiskEdgeType.EXITS_THROUGH
    assert edge(graph, market_node_id("a"), oracle_node_id("a")).loss_share == D(1)

    position = nodes[POSITION]
    assert position.metrics["solvency_scenario"] == "B_degraded"
    assert position.metrics["solvency_w_pool"] == D("0.01")
    expected = market["solvency_bad_debt"] * D("0.01")
    assert abs(position.metrics["solvency_expected_loss"] - expected) < D("1e-16")
    assert position.metrics["solvency_vulnerable"] is True
    assert position.metrics["solvency_r_panic"] == R_PANIC
    assert position.metric_provenance["solvency_r_panic"].kind is MetricProvenanceKind.ASSUMPTION
    assert edge(graph, POSITION, market_node_id("a")).loss_share == D("0.01")


def test_healthy_market_reports_zero_loss() -> None:
    graph = annotate_solvency(market_graph(), {market_id("a"): inputs("a", q=D("10"))}, x_in_pool_units=D("10000"))
    position = by_id(graph)[POSITION].metrics
    assert position["solvency_expected_loss"] == D(0)
    assert position["solvency_vulnerable"] is False


def test_scenario_a_market_loses_x_times_one_minus_r_panic() -> None:
    graph = annotate_solvency(
        market_graph(), {market_id("a"): inputs("a", q=D("600"), r=D("1000"))}, x_in_pool_units=D("10000")
    )
    nodes = by_id(graph)
    assert nodes[market_node_id("a")].metrics["solvency_scenario"] == "A_systemic"
    assert nodes[market_node_id("a")].metrics["solvency_bad_debt"] is None
    assert nodes[POSITION].metrics["solvency_expected_loss"] == D("7000")
    # Prices are not inputs of scenario A.
    assert edge(graph, market_node_id("a"), oracle_node_id("a")).loss_share is None


def test_single_price_source_is_used_with_a_low_confidence_warning() -> None:
    graph = annotate_solvency(
        market_graph(), {market_id("a"): inputs("a", q=D("10"), spot=None)}, x_in_pool_units=D("10000")
    )
    oracle = by_id(graph)[oracle_node_id("a")].metrics
    assert oracle["solvency_price_sources"] == "morpho_oracle"
    assert oracle["solvency_p_real"] == D("2000")
    assert any("one price source" in warning for warning in graph.completeness_warnings)


def test_missing_depth_makes_the_position_unavailable() -> None:
    graph = annotate_solvency(
        market_graph(), {market_id("a"): inputs("a", q=D("10"), r=None)}, x_in_pool_units=D("10000")
    )
    nodes = by_id(graph)
    assert nodes[market_node_id("a")].metrics["solvency_scenario"] == "unavailable"
    assert nodes[market_node_id("a")].metrics["solvency_missing"] == "r"
    assert nodes[POSITION].metrics["solvency_scenario"] == "unavailable"
    assert nodes[POSITION].metrics["solvency_expected_loss"] is None
    assert nodes[POSITION].metrics["solvency_vulnerable"] is None
    assert any("Solvency model unavailable" in warning for warning in graph.completeness_warnings)


def test_expired_oracle_price_is_not_used() -> None:
    graph = annotate_solvency(
        market_graph(),
        {market_id("a"): inputs("a", q=D("10"), spot=None, oracle_freshness=FreshnessStatus.EXPIRED)},
        x_in_pool_units=D("10000"),
    )
    market = by_id(graph)[market_node_id("a")].metrics
    assert market["solvency_scenario"] == "unavailable"
    assert market["solvency_missing"] == "p_contract,p_real"


def test_market_without_inputs_is_unavailable() -> None:
    graph = annotate_solvency(market_graph(), {}, x_in_pool_units=D("10000"))
    assert by_id(graph)[POSITION].metrics["solvency_scenario"] == "unavailable"
    assert by_id(graph)[market_node_id("a")].metrics["solvency_missing"] == "q"


def test_bounded_liquidation_zone_adds_a_warning() -> None:
    graph = annotate_solvency(
        market_graph(), {market_id("a"): inputs("a", q=D("10"), bound=True)}, x_in_pool_units=D("10000")
    )
    assert by_id(graph)[market_node_id("a")].metrics["solvency_q_bound"] is True
    assert any("worst case at LLTV" in warning for warning in graph.completeness_warnings)


def test_unconvertible_numeraire_keeps_the_loss_but_not_w_pool() -> None:
    graph = annotate_solvency(market_graph(), {market_id("a"): inputs("a", q=D("10"))}, x_in_pool_units=None)
    position = by_id(graph)[POSITION].metrics
    assert position["solvency_w_pool"] is None
    assert position["solvency_expected_loss"] == D(0)
    assert edge(graph, POSITION, market_node_id("a")).loss_share is None


# Vault lender


def test_vault_aggregates_markets_on_vault_and_position() -> None:
    graph = annotate_solvency(
        vault_graph(),
        {
            market_id("a"): inputs("a", q=D("0")),
            market_id("b"): inputs("b", q=D("40"), r=D("100"), spot=D("1800")),
        },
        x_in_pool_units=D("3000"),
    )
    nodes = by_id(graph)
    b_bad_debt = nodes[market_node_id("b")].metrics["solvency_bad_debt"]
    vault = nodes[VAULT].metrics
    assert vault["solvency_scenario"] == "B_degraded"
    vault_loss = exact(lambda: b_bad_debt * D("0.2"))
    assert abs(vault["solvency_bad_debt"] - vault_loss) < D("1e-15")
    assert abs(vault["solvency_bad_debt_share"] - exact(lambda: vault_loss / D("300000"))) < D("1e-17")
    position = nodes[POSITION].metrics
    assert position["solvency_w_pool"] == D("0.01")
    assert abs(position["solvency_expected_loss"] - exact(lambda: vault_loss * D("0.01"))) < D("1e-15")
    assert vault["solvency_expected_loss"] == position["solvency_expected_loss"]
    assert edge(graph, POSITION, VAULT).loss_share == D("0.01")
    assert edge(graph, VAULT, market_node_id("b")).loss_share == D("0.2")
    # The vault supplies more than market a reports; the share is capped at 1 with a warning.
    assert edge(graph, VAULT, market_node_id("a")).loss_share == D(1)
    # exposure_weight is untouched: scenarios still propagate by allocation weight.
    assert edge(graph, VAULT, market_node_id("b")).exposure_weight == D("0.667")


def test_vault_with_an_unmodelled_allocation_is_unavailable() -> None:
    graph = annotate_solvency(
        vault_graph(unmodelled=True),
        {market_id("a"): inputs("a", q=D("0")), market_id("b"): inputs("b", q=D("0"))},
        x_in_pool_units=D("3000"),
    )
    nodes = by_id(graph)
    assert nodes[VAULT].metrics["solvency_scenario"] == "unavailable"
    assert nodes[POSITION].metrics["solvency_expected_loss"] is None


# Borrower


def test_borrower_graph_is_marked_not_applicable_and_otherwise_unchanged() -> None:
    original = market_graph(PositionMode.BORROWER)
    graph = annotate_solvency(original, {market_id("a"): inputs("a", q=D("10"))}, x_in_pool_units=None)
    nodes = by_id(graph)
    assert nodes[POSITION].metrics["solvency_model"] == "not_applicable"
    assert "solvency_q" not in nodes[market_node_id("a")].metrics
    assert graph.edges == original.edges
    assert any("lender-only" in warning for warning in graph.completeness_warnings)
