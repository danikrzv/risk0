from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analysis.scenario_engine import ScenarioEngine
from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    PositionMode,
    ProbabilityEstimate,
    ProbabilityMethod,
    RiskEdge,
    RiskEdgeType,
    RiskGraph,
    RiskNode,
    RiskNodeType,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.scenario import (
    ASSET_IMPAIRMENT,
    LIQUIDATION_INCENTIVE,
    LIQUIDATION_TRIGGER,
    LIQUIDITY_SHORTFALL,
    ORACLE_FAILURE_TRIGGER,
    PROTOCOL_IMPAIRMENT,
    SCENARIO_DEFINITIONS,
    ScenarioSeverityError,
    WITHDRAWAL_SHORTFALL_TRIGGER,
    liquidation_incentive_factor,
)


OBSERVED_AT = datetime(2026, 10, 7, 12, tzinfo=UTC)
POSITION = "position:1:analysis"
VAULT = "vault:1:0x00000000000000000000000000000000000000a1"
MARKET_A = "market:1:0x" + "a" * 64
MARKET_B = "market:1:0x" + "b" * 64
MORPHO = "protocol:1:morpho"
ETHEREUM = "chain:1:ethereum"
USDC = "asset:1:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "asset:1:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
CBBTC = "asset:1:0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
ORACLE_A = "oracle:1:0x00000000000000000000000000000000000000c1"
ORACLE_B = "oracle:1:0x00000000000000000000000000000000000000c2"
POOL = "pool:1:0x00000000000000000000000000000000000000d1"


@dataclass(frozen=True)
class StubPosition:
    mode: PositionMode
    entity_id: str
    amount: Decimal
    numeraire: str = "USD"
    collateral_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    ltv: Decimal | None = None
    collateral_value: Decimal | None = None
    debt_value: Decimal | None = None
    lltv: Decimal | None = None


def node(
    node_id: str,
    *,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    **metrics: Decimal | str | bool | None,
) -> RiskNode:
    node_type = RiskNodeType(node_id.split(":", 1)[0])
    return RiskNode(
        node_id=node_id,
        node_type=node_type,
        label=str(metrics.get("symbol") or node_id),
        metrics=metrics,
        provenance="test fixture",
        freshness=freshness,
        observed_at=OBSERVED_AT,
    )


def edge(
    source: str,
    target: str,
    relationship: RiskEdgeType,
    weight: str | None = None,
) -> RiskEdge:
    return RiskEdge(
        source=source,
        target=target,
        relationship=relationship,
        exposure_weight=None if weight is None else Decimal(weight),
    )


def graph(nodes: list[RiskNode], edges: list[RiskEdge], *warnings: str) -> RiskGraph:
    return RiskGraph(
        nodes=tuple(nodes),
        edges=tuple(edges),
        generated_at=OBSERVED_AT,
        completeness_warnings=warnings,
    )


def probability(scenario_id: ScenarioId, central: str = "0.02") -> ProbabilityEstimate:
    return ProbabilityEstimate(
        method=ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=Decimal("0.01"),
        central=Decimal(central),
        high=Decimal("0.03"),
        confidence=ConfidenceLevel.HIGH,
        horizon_days=30,
        source="https://example.test/history",
        retrieved_at=OBSERVED_AT,
        freshness=FreshnessStatus.FRESH,
        explanation="fixture estimate",
    )


def all_probabilities() -> dict[str, ProbabilityEstimate]:
    return {scenario_id.value: probability(scenario_id) for scenario_id in ScenarioId}


def select(scenario_id: ScenarioId, severity: str | None = None) -> ScenarioSelection:
    if severity is None:
        severity_value = SCENARIO_DEFINITIONS[scenario_id].default_severity
    else:
        severity_value = Decimal(severity)
    return ScenarioSelection(scenario_id=scenario_id, severity=severity_value)


def run_one(position, risk_graph, selection, probabilities=None):
    if probabilities is None:
        probabilities = all_probabilities()
    (result,) = ScenarioEngine().run(position, risk_graph, (selection,), probabilities)
    return result


def lender(amount: str = "1000") -> StubPosition:
    return StubPosition(
        mode=PositionMode.LENDER,
        entity_id="test-usdc-vault",
        amount=Decimal(amount),
    )


def borrower(collateral: str, debt: str, lltv: str = "0.86") -> StubPosition:
    collateral_value = Decimal(collateral)
    debt_value = Decimal(debt)
    return StubPosition(
        mode=PositionMode.BORROWER,
        entity_id="weth-usdc-860",
        amount=collateral_value,
        collateral_amount=Decimal("1"),
        debt_amount=debt_value,
        ltv=debt_value / collateral_value,
        collateral_value=collateral_value,
        debt_value=debt_value,
        lltv=Decimal(lltv),
    )


def vault_graph(
    weight_a: str,
    weight_b: str,
    *,
    collateral_a: str = WETH,
    collateral_b: str = CBBTC,
    available_liquidity: Decimal | None = Decimal("5000"),
    pool_depth: Decimal | None = Decimal("2000000"),
) -> RiskGraph:
    """USDC vault lending into two markets; Morpho and Ethereum are shared."""
    collateral_nodes = {
        WETH: node(WETH, symbol="WETH"),
        CBBTC: node(CBBTC, symbol="cbBTC"),
    }
    return graph(
        [
            node(POSITION),
            node(VAULT, symbol="steakUSDC", available_liquidity=available_liquidity),
            node(MARKET_A, lltv=Decimal("0.86")),
            node(MARKET_B, lltv=Decimal("0.86")),
            node(USDC, symbol="USDC"),
            *{key: collateral_nodes[key] for key in (collateral_a, collateral_b)}.values(),
            node(ORACLE_A, age_seconds=Decimal("60"), deviation=Decimal("0.001")),
            node(ORACLE_B, age_seconds=Decimal("60"), deviation=Decimal("0.001")),
            node(MORPHO, symbol="Morpho"),
            node(ETHEREUM),
            node(POOL, liquidity_depth=pool_depth),
        ],
        [
            edge(POSITION, VAULT, RiskEdgeType.LENDS),
            edge(POSITION, POOL, RiskEdgeType.EXITS_THROUGH),
            edge(VAULT, USDC, RiskEdgeType.LENDS),
            edge(VAULT, MARKET_A, RiskEdgeType.ALLOCATED_TO, weight_a),
            edge(VAULT, MARKET_B, RiskEdgeType.ALLOCATED_TO, weight_b),
            edge(VAULT, MORPHO, RiskEdgeType.DEPLOYED_ON),
            edge(MARKET_A, USDC, RiskEdgeType.LENDS),
            edge(MARKET_A, collateral_a, RiskEdgeType.USES_COLLATERAL),
            edge(MARKET_A, ORACLE_A, RiskEdgeType.USES_ORACLE),
            edge(MARKET_A, MORPHO, RiskEdgeType.DEPLOYED_ON),
            edge(MARKET_B, USDC, RiskEdgeType.LENDS),
            edge(MARKET_B, collateral_b, RiskEdgeType.USES_COLLATERAL),
            edge(MARKET_B, ORACLE_B, RiskEdgeType.USES_ORACLE),
            edge(MARKET_B, MORPHO, RiskEdgeType.DEPLOYED_ON),
            edge(MORPHO, ETHEREUM, RiskEdgeType.DEPLOYED_ON),
            edge(POOL, USDC, RiskEdgeType.EXITS_THROUGH),
        ],
    )


def market_borrow_graph(
    *, oracle_age: str = "60", oracle_deviation: str = "0.001"
) -> RiskGraph:
    """Borrower in weth-usdc-860: WETH collateral, USDC debt."""
    return graph(
        [
            node(POSITION),
            node(MARKET_A, lltv=Decimal("0.86")),
            node(USDC, symbol="USDC"),
            node(WETH, symbol="WETH"),
            node(
                ORACLE_A,
                age_seconds=Decimal(oracle_age),
                deviation=Decimal(oracle_deviation),
            ),
            node(MORPHO, symbol="Morpho"),
            node(ETHEREUM),
            node(POOL, liquidity_depth=Decimal("1000000")),
        ],
        [
            edge(POSITION, MARKET_A, RiskEdgeType.BORROWS),
            edge(POSITION, USDC, RiskEdgeType.BORROWS),
            edge(POSITION, WETH, RiskEdgeType.USES_COLLATERAL),
            edge(POSITION, POOL, RiskEdgeType.EXITS_THROUGH),
            edge(MARKET_A, USDC, RiskEdgeType.LENDS),
            edge(MARKET_A, WETH, RiskEdgeType.USES_COLLATERAL),
            edge(MARKET_A, ORACLE_A, RiskEdgeType.USES_ORACLE),
            edge(MARKET_A, MORPHO, RiskEdgeType.DEPLOYED_ON),
            edge(MORPHO, ETHEREUM, RiskEdgeType.DEPLOYED_ON),
        ],
    )


# Lender scenarios


def test_lender_vault_impact_is_weighted_by_allocation_exposure():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6"),
        select(ScenarioId.COLLATERAL_SHOCK, "0.10"),
    )

    assert result.impact == Decimal("40")
    assert result.impact_components[ASSET_IMPAIRMENT] == Decimal("40")
    assert result.hard_triggers == ()
    assert result.affected_path == (POSITION, VAULT, MARKET_A, WETH)
    assert MARKET_B not in result.affected_path


def test_lender_direct_market_supply_is_fully_exposed():
    risk_graph = graph(
        [node(POSITION), node(MARKET_A), node(USDC, symbol="USDC"), node(WETH, symbol="WETH")],
        [
            edge(POSITION, MARKET_A, RiskEdgeType.LENDS),
            edge(MARKET_A, USDC, RiskEdgeType.LENDS),
            edge(MARKET_A, WETH, RiskEdgeType.USES_COLLATERAL),
        ],
    )

    result = run_one(lender("1000"), risk_graph, select(ScenarioId.STABLECOIN_DEPEG))

    assert result.impact == Decimal("100")
    assert result.affected_path == (POSITION, MARKET_A, USDC)


def test_zero_weight_allocation_contributes_no_loss_and_is_not_on_the_path():
    result = run_one(
        lender("1000"),
        vault_graph("0", "1"),
        select(ScenarioId.COLLATERAL_SHOCK, "0.10"),
    )

    assert result.impact == Decimal("0")
    assert result.impact_components[ASSET_IMPAIRMENT] == Decimal("0")
    assert result.affected_path == ()
    assert MARKET_A not in result.affected_path


def test_lender_liquidity_reduction_is_reported_separately_from_permanent_loss():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6", available_liquidity=Decimal("2000")),
        select(ScenarioId.LIQUIDITY_COLLAPSE, "0.80"),
    )

    assert result.impact == Decimal("0")
    assert result.impact_components[LIQUIDITY_SHORTFALL] == Decimal("600")
    assert result.stressed_values["available_liquidity"] == Decimal("2000")
    assert result.stressed_values["stressed_available_liquidity"] == Decimal("400")
    assert result.stressed_values["withdrawable_amount"] == Decimal("400")
    assert result.stressed_values["liquidity_shortfall_is_realized_loss"] is False
    assert result.stressed_values["stressed_exit_pool_depth"] == Decimal("400000")
    assert WITHDRAWAL_SHORTFALL_TRIGGER not in result.hard_triggers
    assert result.affected_path == (POSITION, POOL, VAULT)


def test_lender_withdrawal_trigger_when_stressed_liquidity_is_below_twenty_percent():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6", available_liquidity=Decimal("500")),
        select(ScenarioId.LIQUIDITY_COLLAPSE, "0.80"),
    )

    assert result.impact == Decimal("0")
    assert result.impact_components[LIQUIDITY_SHORTFALL] == Decimal("900")
    assert result.hard_triggers == (WITHDRAWAL_SHORTFALL_TRIGGER,)


def test_missing_lender_liquidity_is_unavailable_not_safe():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6", available_liquidity=None),
        select(ScenarioId.LIQUIDITY_COLLAPSE),
    )

    assert result.stressed_values["stressed_available_liquidity"] is None
    assert result.confidence == ConfidenceLevel.UNAVAILABLE


def test_shared_collateral_dependency_via_two_markets_is_counted_once():
    result = run_one(
        lender("1000"),
        vault_graph("0.5", "0.5", collateral_a=WETH, collateral_b=WETH),
        select(ScenarioId.COLLATERAL_SHOCK, "0.10"),
    )

    assert result.impact == Decimal("100")
    assert result.affected_path == (POSITION, VAULT, MARKET_A, MARKET_B, WETH)
    assert result.affected_path.count(WETH) == 1


def test_shared_protocol_reached_by_vault_and_markets_is_counted_once():
    result = run_one(
        lender("1000"),
        vault_graph("0.5", "0.5"),
        select(ScenarioId.PROTOCOL_IMPAIRMENT, "0.10"),
    )

    assert result.impact == Decimal("100")
    assert result.impact_components[PROTOCOL_IMPAIRMENT] == Decimal("100")
    assert result.affected_path.count(MORPHO) == 1
    assert result.affected_path[0] == POSITION
    assert result.affected_path[-1] == MORPHO


def test_lender_oracle_failure_hits_markets_using_the_oracle():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6"),
        select(ScenarioId.ORACLE_FAILURE, "0.05"),
    )

    assert result.impact == Decimal("50")
    assert result.impact_components[PROTOCOL_IMPAIRMENT] == Decimal("50")
    assert ORACLE_FAILURE_TRIGGER not in result.hard_triggers


def test_missing_allocation_weight_is_treated_as_full_exposure():
    risk_graph = vault_graph("0.4", "0.6")
    edges = tuple(
        edge(item.source, item.target, item.relationship)
        if item.target == MARKET_A
        else item
        for item in risk_graph.edges
    )
    risk_graph = risk_graph.model_copy(update={"edges": edges})

    result = run_one(lender("1000"), risk_graph, select(ScenarioId.COLLATERAL_SHOCK, "0.10"))

    assert result.impact == Decimal("100")
    assert result.confidence != ConfidenceLevel.HIGH


# Borrower scenarios


def test_borrower_collateral_shock_recalculates_stressed_ltv():
    position = borrower("1000", "500")

    result = run_one(position, market_borrow_graph(), select(ScenarioId.COLLATERAL_SHOCK))

    expected = position.debt_value / (position.collateral_value * Decimal("0.85"))
    assert result.stressed_values["stressed_ltv"] == expected
    assert result.stressed_values["stressed_collateral_value"] == Decimal("850")
    assert result.stressed_values["stressed_debt_value"] == Decimal("500")
    assert result.hard_triggers == ()
    assert result.impact_components[ASSET_IMPAIRMENT] == Decimal("150")
    assert result.impact_components[LIQUIDATION_INCENTIVE] == Decimal("0")
    assert result.impact == Decimal("150")
    assert result.affected_path == (POSITION, MARKET_A, WETH)


def test_stressed_ltv_equal_to_lltv_triggers_liquidation():
    position = borrower("1000", "731")

    result = run_one(position, market_borrow_graph(), select(ScenarioId.COLLATERAL_SHOCK))

    assert result.stressed_values["stressed_ltv"] == Decimal("0.86")
    assert result.hard_triggers == (LIQUIDATION_TRIGGER,)
    factor = liquidation_incentive_factor(Decimal("0.86"))
    seized = Decimal("731") * factor
    incentive = seized - seized / factor
    assert result.stressed_values["liquidation_incentive_factor"] == factor
    assert result.stressed_values["collateral_sold"] == seized
    assert result.stressed_values["residual_collateral"] == Decimal("850") - seized
    assert result.stressed_values["residual_debt"] == Decimal("731") - seized / factor
    assert result.impact_components[LIQUIDATION_INCENTIVE] == incentive
    assert result.impact == Decimal("150") + incentive


def test_stressed_ltv_one_base_unit_below_lltv_does_not_trigger():
    position = borrower("1000.000000000000000001", "731")

    result = run_one(position, market_borrow_graph(), select(ScenarioId.COLLATERAL_SHOCK))

    assert result.stressed_values["stressed_ltv"] < Decimal("0.86")
    assert result.hard_triggers == ()
    assert result.impact_components[LIQUIDATION_INCENTIVE] == Decimal("0")


def test_liquidation_incentive_factor_matches_morpho_formula():
    assert liquidation_incentive_factor(Decimal("0.86")) == Decimal(1) / Decimal("0.958")
    assert liquidation_incentive_factor(Decimal("0.385")) == Decimal("1.15")


def test_loan_asset_depeg_reduces_stressed_debt_value():
    position = borrower("1000", "500")

    result = run_one(position, market_borrow_graph(), select(ScenarioId.STABLECOIN_DEPEG))

    assert result.stressed_values["stressed_debt_value"] == Decimal("450.0")
    assert result.stressed_values["stressed_ltv"] == Decimal("450.0") / Decimal("1000")
    assert result.impact == Decimal("0")
    assert USDC in result.affected_path


def test_borrower_oracle_failure_can_force_liquidation_and_flags_live_breach():
    position = borrower("1000", "820")

    result = run_one(
        position,
        market_borrow_graph(oracle_age="3600"),
        select(ScenarioId.ORACLE_FAILURE, "0.05"),
    )

    assert result.stressed_values["oracle_collateral_value"] == Decimal("950.00")
    assert result.stressed_values["stressed_collateral_value"] == Decimal("1000")
    assert result.hard_triggers == (LIQUIDATION_TRIGGER, ORACLE_FAILURE_TRIGGER)
    assert result.impact_components[ASSET_IMPAIRMENT] == Decimal("0")
    assert result.impact_components[LIQUIDATION_INCENTIVE] > Decimal("0")
    assert result.affected_path == (POSITION, MARKET_A, ORACLE_A)


def test_borrower_without_lltv_or_values_is_unavailable_not_safe():
    position = StubPosition(
        mode=PositionMode.BORROWER,
        entity_id="weth-usdc-860",
        amount=Decimal("1000"),
        collateral_amount=Decimal("1"),
        debt_amount=Decimal("500"),
        ltv=Decimal("0.5"),
    )

    result = run_one(position, market_borrow_graph(), select(ScenarioId.COLLATERAL_SHOCK))

    assert result.confidence == ConfidenceLevel.UNAVAILABLE
    assert result.stressed_values["stressed_ltv"] is None
    assert "unavailable" in result.explanation.lower()


# General behaviour


def test_missing_probability_is_explicitly_unavailable_never_zero():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6"),
        select(ScenarioId.COLLATERAL_SHOCK),
        probabilities={},
    )

    assert result.probability.method == ProbabilityMethod.UNAVAILABLE
    assert result.probability.low is None
    assert result.probability.central is None
    assert result.probability.high is None
    assert result.probability.confidence == ConfidenceLevel.UNAVAILABLE
    assert result.probability.horizon_days == 30
    assert result.probability.explanation
    assert result.confidence == ConfidenceLevel.UNAVAILABLE


def test_supplied_probability_is_passed_through():
    supplied = probability(ScenarioId.COLLATERAL_SHOCK, central="0.025")

    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6"),
        select(ScenarioId.COLLATERAL_SHOCK),
        probabilities={ScenarioId.COLLATERAL_SHOCK.value: supplied},
    )

    assert result.probability == supplied
    assert result.confidence == ConfidenceLevel.HIGH


def test_stale_path_node_lowers_confidence():
    risk_graph = vault_graph("0.4", "0.6")
    nodes = tuple(
        item.model_copy(update={"freshness": FreshnessStatus.EXPIRED})
        if item.node_id == WETH
        else item
        for item in risk_graph.nodes
    )
    risk_graph = risk_graph.model_copy(update={"nodes": nodes})

    result = run_one(lender("1000"), risk_graph, select(ScenarioId.COLLATERAL_SHOCK))

    assert result.confidence == ConfidenceLevel.UNAVAILABLE


@pytest.mark.parametrize(
    ("scenario_id", "severity"),
    [
        (ScenarioId.STABLECOIN_DEPEG, "0"),
        (ScenarioId.STABLECOIN_DEPEG, "1.01"),
        (ScenarioId.COLLATERAL_SHOCK, "-0.15"),
        (ScenarioId.ORACLE_FAILURE, "0.04"),
        (ScenarioId.ORACLE_FAILURE, "1"),
        (ScenarioId.LIQUIDITY_COLLAPSE, "1.5"),
        (ScenarioId.PROTOCOL_IMPAIRMENT, "0.05"),
        (ScenarioId.PROTOCOL_IMPAIRMENT, "1.0001"),
    ],
)
def test_out_of_range_severity_is_rejected(scenario_id, severity):
    with pytest.raises(ScenarioSeverityError):
        ScenarioEngine().run(
            lender("1000"),
            vault_graph("0.4", "0.6"),
            (select(scenario_id, severity),),
            all_probabilities(),
        )


def test_protocol_impairment_accepts_total_loss():
    result = run_one(
        lender("1000"),
        vault_graph("0.4", "0.6"),
        select(ScenarioId.PROTOCOL_IMPAIRMENT, "1"),
    )

    assert result.impact == Decimal("1000")


def test_default_severities_match_the_spec():
    defaults = {
        scenario_id: definition.default_severity
        for scenario_id, definition in SCENARIO_DEFINITIONS.items()
    }

    assert defaults == {
        ScenarioId.STABLECOIN_DEPEG: Decimal("0.10"),
        ScenarioId.COLLATERAL_SHOCK: Decimal("0.15"),
        ScenarioId.ORACLE_FAILURE: Decimal("0.05"),
        ScenarioId.LIQUIDITY_COLLAPSE: Decimal("0.80"),
        ScenarioId.PROTOCOL_IMPAIRMENT: Decimal("0.10"),
    }


def test_returns_one_result_per_selection_in_order():
    selections = (
        select(ScenarioId.PROTOCOL_IMPAIRMENT),
        select(ScenarioId.STABLECOIN_DEPEG),
        select(ScenarioId.LIQUIDITY_COLLAPSE),
        select(ScenarioId.COLLATERAL_SHOCK),
        select(ScenarioId.ORACLE_FAILURE),
    )

    results = ScenarioEngine().run(
        lender("1000"), vault_graph("0.4", "0.6"), selections, all_probabilities()
    )

    assert tuple(result.scenario_id for result in results) == tuple(
        selection.scenario_id.value for selection in selections
    )
    for result in results:
        assert result.impact == sum(
            (
                result.impact_components[name]
                for name in (ASSET_IMPAIRMENT, LIQUIDATION_INCENTIVE, PROTOCOL_IMPAIRMENT)
            ),
            Decimal(0),
        )


def test_results_are_deterministic_regardless_of_edge_order():
    risk_graph = vault_graph("0.4", "0.6")
    reversed_graph = risk_graph.model_copy(
        update={
            "edges": tuple(reversed(risk_graph.edges)),
            "nodes": tuple(reversed(risk_graph.nodes)),
        }
    )
    selection = select(ScenarioId.PROTOCOL_IMPAIRMENT)

    assert run_one(lender(), risk_graph, selection) == run_one(lender(), reversed_graph, selection)


def test_graph_without_a_single_position_node_is_rejected():
    risk_graph = graph([node(MARKET_A)], [])

    with pytest.raises(ValueError, match="position"):
        ScenarioEngine().run(
            lender(), risk_graph, (select(ScenarioId.STABLECOIN_DEPEG),), all_probabilities()
        )
