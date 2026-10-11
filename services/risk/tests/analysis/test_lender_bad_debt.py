"""Lender price events: held-claim impairment plus collateral bad debt."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
from app.domain.bad_debt import (
    METHOD_POSITIONS,
    METHOD_WORST_CASE,
    bad_debt_fraction_at_lltv,
    build_distribution,
    estimate_bad_debt,
)
from app.domain.scenario import ASSET_IMPAIRMENT


D = Decimal
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
POSITION = "position:1:lender"
VAULT = "vault:1:0x00000000000000000000000000000000000000a1"
MORPHO = "protocol:1:morpho"
USDC = "asset:1:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "asset:1:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WSTETH = "asset:1:0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
WBTC = "asset:1:0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
CBBTC = "asset:1:0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
ETH = "asset:1:underlying-eth"
USD = "asset:1:underlying-usd-reserves"
WUSDC = "asset:1:0x00000000000000000000000000000000000000f1"
SYMBOLS = {
    USDC: "USDC",
    WETH: "WETH",
    WSTETH: "wstETH",
    WBTC: "WBTC",
    CBBTC: "cbBTC",
    ETH: "ETH",
    USD: "USD",
    WUSDC: "wUSDC",
}
LLTV = D("0.86")
SUPPLY = D("1000000")
BORROW = D("800000")


def market_id(letter: str) -> str:
    return "0x" + letter * 64


def market_node(letter: str) -> str:
    return f"market:1:{market_id(letter)}"


@dataclass(frozen=True)
class Lender:
    amount: Decimal
    numeraire: str = "USDC"
    mode: PositionMode = PositionMode.LENDER
    entity_id: str = "test"
    collateral_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    ltv: Decimal | None = None
    collateral_value: Decimal | None = None
    debt_value: Decimal | None = None
    lltv: Decimal | None = None


def node(identifier: str, **metrics) -> RiskNode:
    return RiskNode(
        node_id=identifier,
        node_type=RiskNodeType(identifier.split(":", 1)[0]),
        label=metrics.pop("label", None) or SYMBOLS.get(identifier, identifier),
        metrics=metrics,
        provenance="test fixture",
        freshness=FreshnessStatus.FRESH,
        observed_at=NOW,
    )


def edge(source, target, relationship, weight=None) -> RiskEdge:
    return RiskEdge(
        source=source,
        target=target,
        relationship=relationship,
        exposure_weight=None if weight is None else D(weight),
    )


class GraphBuilder:
    def __init__(self) -> None:
        self.nodes = {POSITION: node(POSITION), MORPHO: node(MORPHO)}
        self.edges: list[RiskEdge] = []

    def asset(self, identifier: str) -> str:
        self.nodes.setdefault(identifier, node(identifier, symbol=SYMBOLS[identifier]))
        return identifier

    def market(self, letter: str, loan: str, collateral: str) -> str:
        identifier = market_node(letter)
        self.nodes[identifier] = node(
            identifier,
            label=f"{SYMBOLS[collateral]} collateral / {SYMBOLS[loan]} debt",
            market_id=market_id(letter),
            lltv=LLTV,
            total_supply_assets=SUPPLY,
            total_borrow_assets=BORROW,
            utilization=BORROW / SUPPLY,
            loan_symbol=SYMBOLS[loan],
        )
        self.edges += [
            edge(identifier, self.asset(loan), RiskEdgeType.LENDS),
            edge(identifier, self.asset(collateral), RiskEdgeType.USES_COLLATERAL),
            edge(identifier, MORPHO, RiskEdgeType.DEPLOYED_ON),
        ]
        return identifier

    def backed_by(self, asset: str, underlying: str) -> None:
        self.asset(asset)
        self.asset(underlying)
        self.edges.append(edge(asset, underlying, RiskEdgeType.BACKED_BY))

    def link(self, source, target, relationship, weight=None) -> None:
        self.edges.append(edge(source, target, relationship, weight))

    def graph(self) -> RiskGraph:
        return RiskGraph(
            nodes=tuple(self.nodes.values()),
            edges=tuple(self.edges),
            generated_at=NOW,
            completeness_warnings=(),
        )


def vault(loan: str, allocations: list[tuple[str, str, str]]) -> RiskGraph:
    """Lender in a vault lending `loan` into (letter, collateral, weight) markets."""
    builder = GraphBuilder()
    builder.nodes[VAULT] = node(VAULT, label="Vault")
    builder.link(POSITION, VAULT, RiskEdgeType.LENDS)
    builder.link(VAULT, builder.asset(loan), RiskEdgeType.LENDS)
    builder.link(VAULT, MORPHO, RiskEdgeType.DEPLOYED_ON)
    for letter, collateral, weight in allocations:
        builder.link(VAULT, builder.market(letter, loan, collateral), RiskEdgeType.ALLOCATED_TO, weight)
    return builder.graph()


def direct(loan: str, collateral: str, builder: GraphBuilder | None = None) -> RiskGraph:
    builder = builder or GraphBuilder()
    builder.link(POSITION, builder.market("a", loan, collateral), RiskEdgeType.LENDS)
    return builder.graph()


def probability() -> ProbabilityEstimate:
    return ProbabilityEstimate(
        method=ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=D("0.01"),
        central=D("0.02"),
        high=D("0.03"),
        confidence=ConfidenceLevel.HIGH,
        horizon_days=30,
        source="https://example.test/history",
        retrieved_at=NOW,
        freshness=FreshnessStatus.FRESH,
        explanation="fixture estimate",
    )


def run(position, graph, scenario, severity, borrowers=None):
    selection = ScenarioSelection(scenario_id=scenario, severity=D(severity))
    (result,) = ScenarioEngine().run(
        position, graph, (selection,), {scenario.value: probability()}, borrowers=borrowers
    )
    return result


def positions(letter: str, *rows):
    """Borrowers of market `letter` as (debt, health factor) rows."""
    return {
        market_id(letter): build_distribution(
            market_id(letter), [(D(debt), D(hf)) for debt, hf in rows], LLTV, complete=True
        )
    }


def fraction(shock: str, letter: str | None = None, *rows) -> Decimal:
    distribution = positions(letter, *rows)[market_id(letter)] if letter else None
    return estimate_bad_debt(
        D(shock), lltv=LLTV, total_supply=SUPPLY, total_borrow=BORROW, distribution=distribution
    ).supplier_loss_fraction


# Collateral shock


def test_market_lender_loses_only_bad_debt_not_the_collateral_fall():
    rows = (("500000", "1.30"), ("300000", "0.95"))
    result = run(
        Lender(D("100000")), direct(USDC, WETH), ScenarioId.COLLATERAL_SHOCK, "0.15",
        positions("a", *rows),
    )

    # HF 0.95 -> collateral/debt 1.1047; x 0.85 = 0.939: 300000 x 0.0610 bad debt.
    expected = D("100000") * fraction("0.15", "a", *rows)
    assert result.impact == expected
    assert D(0) < result.impact < D("15000")  # the old 1:1 charge
    assert result.impact_components[ASSET_IMPAIRMENT] == expected
    values = result.stressed_values
    assert values["held_claim_exposure"] == 0
    assert values["bad_debt_loss"] == expected
    assert values[f"bad_debt.{market_id('a')[:10]}.method"] == METHOD_POSITIONS
    assert result.confidence == ConfidenceLevel.HIGH
    assert "1 of 2 borrowers below 1.0 collateral/debt" in result.explanation
    assert "no liquidation executed" in result.explanation


def test_no_bad_debt_when_every_borrower_is_healthy():
    result = run(
        Lender(D("100000")), direct(USDC, WETH), ScenarioId.COLLATERAL_SHOCK, "0.15",
        positions("a", ("800000", "1.5")),
    )

    assert result.impact == 0
    assert result.stressed_values["impact_complete"] is True


def test_vault_lender_sums_market_bad_debt_by_allocation_weight():
    graph = vault(USDC, [("a", WETH, "0.3"), ("b", WBTC, "0.5"), ("c", CBBTC, "0.2")])
    rows_a = (("800000", "0.9"),)
    rows_b = (("400000", "1.0"), ("400000", "2.0"))
    borrowers = {**positions("a", *rows_a), **positions("b", *rows_b)}

    result = run(Lender(D("1000")), graph, ScenarioId.COLLATERAL_SHOCK, "0.30", borrowers)

    loss_a = D("1000") * D("0.3") * fraction("0.30", "a", *rows_a)
    loss_b = D("1000") * D("0.5") * fraction("0.30", "b", *rows_b)
    assert result.impact == loss_a + loss_b
    values = result.stressed_values
    assert values["bad_debt_markets"] == 2  # cbBTC is not a shocked collateral
    assert values[f"bad_debt.{market_id('a')[:10]}.exposure"] == D("0.3")
    assert values[f"bad_debt.{market_id('b')[:10]}.loss"] == loss_b
    assert values["bad_debt_exposure"] == D("0.8")
    assert market_node("c") not in result.affected_path


def test_collateral_markets_lists_shocked_markets_by_exposure():
    graph = vault(USDC, [("a", WETH, "0.3"), ("b", WBTC, "0.5"), ("c", CBBTC, "0.2")])
    selections = (
        ScenarioSelection(scenario_id=ScenarioId.PROTOCOL_IMPAIRMENT, severity=D("0.1")),
        ScenarioSelection(scenario_id=ScenarioId.COLLATERAL_SHOCK, severity=D("0.15")),
    )

    assert ScenarioEngine().collateral_markets(graph, selections) == (market_id("b"), market_id("a"))


def test_positions_unavailable_uses_the_worst_case_bound_and_lowers_confidence():
    result = run(Lender(D("100000")), direct(USDC, WETH), ScenarioId.COLLATERAL_SHOCK, "0.30")

    bound = bad_debt_fraction_at_lltv(D("0.30"), LLTV) * BORROW / SUPPLY
    assert result.impact == D("100000") * bound
    assert result.stressed_values["bad_debt_bounded_markets"] == 1
    assert result.stressed_values[f"bad_debt.{market_id('a')[:10]}.method"] == METHOD_WORST_CASE
    # The same clean graph is HIGH with positions; the bound lowers it one level.
    assert result.confidence == ConfidenceLevel.MEDIUM
    assert "worst-case bound: borrower positions unavailable" in result.explanation.lower()
    assert result.stressed_values["impact_complete"] is True


def test_bound_below_the_lltv_cushion_costs_nothing_but_is_still_flagged():
    result = run(Lender(D("100000")), direct(USDC, WETH), ScenarioId.COLLATERAL_SHOCK, "0.10")

    assert result.impact == 0
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_loan_asset_sharing_the_collaterals_underlying_moves_with_it():
    # WETH debt against wstETH collateral: both are ETH, an ETH crash moves both.
    builder = GraphBuilder()
    builder.backed_by(WSTETH, ETH)
    builder.backed_by(WETH, ETH)
    graph = direct(WETH, WSTETH, builder)

    result = run(
        Lender(D("100"), numeraire="WETH"), graph, ScenarioId.COLLATERAL_SHOCK, "0.40",
        positions("a", ("800000", "1.0")),
    )

    assert result.impact == 0
    assert result.stressed_values[f"bad_debt.{market_id('a')[:10]}.collateral_shock"] == 0
    assert "no relative fall" in result.explanation


def test_loan_asset_without_shared_underlying_takes_the_full_relative_fall():
    result = run(
        Lender(D("100"), numeraire="WETH"), direct(WETH, WBTC), ScenarioId.COLLATERAL_SHOCK, "0.40",
        positions("a", ("800000", "1.0")),
    )

    assert result.impact == D("100") * fraction("0.40", "a", ("800000", "1.0"))
    assert result.impact > 0


# Stablecoin depeg: held claim vs collateral


def test_depeg_of_the_held_loan_asset_is_a_one_to_one_claim_impairment():
    result = run(
        Lender(D("1000")), direct(USDC, WETH), ScenarioId.STABLECOIN_DEPEG, "0.10",
        positions("a", ("800000", "1.0")),
    )

    # The held USDC claim loses 10%; borrowers' WETH collateral is untouched (no bad debt).
    assert result.impact == D("100")
    assert result.stressed_values["held_claim_impairment"] == D("100")
    assert result.stressed_values["bad_debt_loss"] == 0
    assert result.stressed_values["bad_debt_markets"] == 0


def test_depeg_of_a_collateral_asset_goes_through_bad_debt():
    rows = (("400000", "0.9"), ("400000", "1.3"))

    result = run(
        Lender(D("100"), numeraire="WETH"), direct(WETH, USDC), ScenarioId.STABLECOIN_DEPEG, "0.20",
        positions("a", *rows),
    )

    assert result.stressed_values["held_claim_exposure"] == 0
    assert result.impact == D("100") * fraction("0.20", "a", *rows)
    assert D(0) < result.impact < D("20")
    assert USDC in result.affected_path


def test_depeg_does_not_co_move_through_a_shared_reference():
    # A USDC depeg is issuer-specific: a loan asset backed by the same USD reserves
    # reference does not fall with it (only a market-price shock co-moves).
    builder = GraphBuilder()
    builder.backed_by(USDC, USD)
    builder.backed_by(WETH, USD)  # artificial: a USD-reserve-backed loan asset
    rows = (("800000", "1.0"),)

    result = run(
        Lender(D("100"), numeraire="WETH"), direct(WETH, USDC, builder),
        ScenarioId.STABLECOIN_DEPEG, "0.30", positions("a", *rows),
    )

    assert result.impact == D("100") * fraction("0.30", "a", *rows)
    assert result.impact > 0


def test_depeg_hitting_loan_and_collateral_together_creates_no_bad_debt():
    # Collateral wrapped-USDC is backed by USDC, the loan asset: both fall together.
    builder = GraphBuilder()
    builder.backed_by(WUSDC, USDC)

    result = run(
        Lender(D("1000")), direct(USDC, WUSDC, builder), ScenarioId.STABLECOIN_DEPEG, "0.10",
        positions("a", ("800000", "1.0")),
    )

    assert result.stressed_values["held_claim_impairment"] == D("100")
    assert result.stressed_values["bad_debt_loss"] == 0
    assert result.impact == D("100")
