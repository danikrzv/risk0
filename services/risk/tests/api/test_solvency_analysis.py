"""End-to-end solvency annotations through `AnalysisService` with injected providers."""

from decimal import Decimal

from app.adapters.morpho import MarketPosition, MarketPositions, MorphoUnavailableError
from app.contracts.models import (
    FreshnessStatus,
    MetricProvenanceKind,
    RiskEdgeType,
    RiskNodeType,
)
from app.domain.snapshots import DataEnvelope
from tests.api.test_analysis import (
    WETH_USDC,
    WSTETH_WETH,
    FakeOracles,
    FakeQuotes,
    RecordedAllocationsOnly,
    analyze,
    assert_contract,
    borrower_request,
    build_service,
    lender_market_request,
    lender_vault_request,
)


D = Decimal
POSITIONS_SOURCE = "https://morpho.test/graphql"
SCENARIOS = [{"scenario_id": "protocol-impairment", "severity": "0.10"}]


class PricedOracles(FakeOracles):
    """Healthy oracle observations that also carry `price()` (loan per collateral)."""

    def __init__(self, clock, price: str | None) -> None:
        super().__init__(clock)
        self.price = None if price is None else D(price)

    def observe(self, market, *, now):
        envelope = super().observe(market, now=now)
        return envelope.model_copy(
            update={"value": envelope.value.model_copy(update={"price": self.price})}
        )


class WithPositions:
    """Recorded Morpho client plus `get_market_positions` rows of (collateral, debt, hf)."""

    def __init__(self, client, clock, rows=(), *, error=None, complete=True) -> None:
        self.client = client
        self.clock = clock
        self.rows = tuple(rows)
        self.error = error
        self.complete = complete
        self.requested: list[str] = []

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_market_positions(self, market_id, *, chain_id=1):
        self.requested.append(market_id)
        if self.error is not None:
            raise self.error
        positions = tuple(
            MarketPosition(
                user=f"0x{index:040x}",
                supply_assets=D(0),
                borrow_assets=D(debt),
                collateral_assets=D(collateral),
                health_factor=None if hf is None else D(hf),
            )
            for index, (collateral, debt, hf) in enumerate(self.rows, start=1)
        )
        return DataEnvelope(
            value=MarketPositions(
                chain_id=chain_id, market_id=market_id, positions=positions, complete=self.complete
            ),
            source=POSITIONS_SOURCE,
            fetched_at=self.clock(),
            freshness=FreshnessStatus.FRESH,
            block_number=26161049,
        )


def nodes_by_type(response, node_type):
    return [node for node in response.graph.nodes if node.node_type is node_type]


def position_metrics(response):
    (position,) = nodes_by_type(response, RiskNodeType.POSITION)
    return position.metrics


def test_market_lender_gets_solvency_metrics_without_changing_the_recommendation(morpho_client, clock):
    market = morpho_client.get_market(WETH_USDC).value
    debt = market.total_borrow_assets
    # One borrower in the liquidation zone (HF 1.02) holds 10 WETH; the rest is healthy.
    morpho = WithPositions(morpho_client, clock, rows=[("10", "21000", "1.02"), ("1000", debt - 21000, "1.6")])
    quotes = FakeQuotes(clock)
    service = build_service(morpho, clock, oracles=PricedOracles(clock, "2500"), quotes=quotes)

    response = analyze(service, lender_market_request(SCENARIOS))
    baseline = analyze(
        build_service(morpho_client, clock, oracles=PricedOracles(clock, "2500")),
        lender_market_request(SCENARIOS),
    )

    assert response.recommendation == baseline.recommendation
    assert response.scenario_results == baseline.scenario_results
    assert morpho.requested == [WETH_USDC]

    (market_node,) = nodes_by_type(response, RiskNodeType.MARKET)
    metrics = market_node.metrics
    assert metrics["solvency_q"] == D("10")
    assert metrics["solvency_borrowers_in_zone"] == D(1)
    assert metrics["solvency_scenario"] == "B_degraded"
    assert metrics["solvency_bad_debt"] == D(0)
    assert market_node.metric_provenance["solvency_q"].source == POSITIONS_SOURCE
    assert market_node.metric_provenance["solvency_q"].block_number == 26161049

    (pool,) = [node for node in nodes_by_type(response, RiskNodeType.POOL) if "solvency_r" in node.metrics]
    # FakeQuotes reports a 0.1% price impact: R = Q * 0.999 / 0.001.
    assert pool.metrics["solvency_r"] == D("9990")
    assert pool.metric_provenance["solvency_r"].source == "uniswap-v3:quoter-v2"
    exit_edge = next(edge for edge in response.graph.edges if edge.target == pool.node_id and edge.source == market_node.node_id)
    assert exit_edge.relationship is RiskEdgeType.EXITS_THROUGH
    assert exit_edge.loss_share == D(1)

    oracle = next(node for node in nodes_by_type(response, RiskNodeType.ORACLE) if "solvency_p_contract" in node.metrics)
    assert oracle.metrics["solvency_p_contract"] == D("2500")
    assert oracle.metrics["solvency_price_sources"] == "morpho_oracle,uniswap_spot"
    assert oracle.metrics["solvency_p_real"] <= D("2500")

    position = position_metrics(response)
    assert position["solvency_scenario"] == "B_degraded"
    assert position["solvency_expected_loss"] == D(0)
    assert position["solvency_vulnerable"] is False
    w_pool = position["solvency_w_pool"]
    assert abs(w_pool - D("100000") / market.total_supply_assets) < D("1e-17")
    lends = next(edge for edge in response.graph.edges if edge.relationship is RiskEdgeType.LENDS and edge.target == market_node.node_id and edge.source.startswith("position"))
    assert lends.loss_share == w_pool
    assert_contract(response.model_dump(mode="json"))


def test_positions_are_fetched_once_for_bad_debt_and_solvency(morpho_client, clock):
    market = morpho_client.get_market(WETH_USDC).value
    morpho = WithPositions(morpho_client, clock, rows=[("1000", market.total_borrow_assets, "1.5")])
    service = build_service(morpho, clock, oracles=PricedOracles(clock, "2500"))

    analyze(service, lender_market_request([{"scenario_id": "collateral-shock", "severity": "0.15"}]))

    assert morpho.requested == [WETH_USDC]


def test_positions_outage_makes_the_solvency_model_unavailable(morpho_client, clock):
    morpho = WithPositions(morpho_client, clock, error=MorphoUnavailableError("down at https://secret"))
    service = build_service(morpho, clock, oracles=PricedOracles(clock, "2500"))

    response = analyze(service, lender_market_request(SCENARIOS))

    position = position_metrics(response)
    assert position["solvency_scenario"] == "unavailable"
    assert position["solvency_expected_loss"] is None
    assert any("Solvency model unavailable" in warning for warning in response.graph.completeness_warnings)
    assert "secret" not in response.model_dump_json()


def test_missing_oracle_price_with_borrowers_in_the_zone_is_unavailable(morpho_client, clock):
    market = morpho_client.get_market(WETH_USDC).value
    morpho = WithPositions(morpho_client, clock, rows=[("10", market.total_borrow_assets, "1.01")])
    service = build_service(morpho, clock, oracles=PricedOracles(clock, None))

    response = analyze(service, lender_market_request(SCENARIOS))

    (market_node,) = nodes_by_type(response, RiskNodeType.MARKET)
    # The Uniswap spot is the only price source left; P_contract is missing.
    assert market_node.metrics["solvency_scenario"] == "unavailable"
    assert "p_contract" in market_node.metrics["solvency_missing"]


def test_vault_lender_aggregates_its_markets(morpho_client, clock):
    morpho = WithPositions(RecordedAllocationsOnly(morpho_client), clock, rows=[("100", "10", "3")])
    service = build_service(morpho, clock, oracles=PricedOracles(clock, "1.2"))

    response = analyze(service, lender_vault_request(scenarios=SCENARIOS))

    (vault,) = nodes_by_type(response, RiskNodeType.VAULT)
    assert vault.metrics["solvency_scenario"] == "B_degraded"
    assert vault.metrics["solvency_bad_debt"] == D(0)
    assert vault.metric_provenance["solvency_bad_debt"].kind is MetricProvenanceKind.DERIVED
    allocated = [edge for edge in response.graph.edges if edge.relationship is RiskEdgeType.ALLOCATED_TO]
    assert allocated and all(edge.loss_share is not None for edge in allocated)
    assert all(edge.exposure_weight is not None for edge in allocated)
    position = position_metrics(response)
    assert position["solvency_expected_loss"] == D(0)
    assert position["solvency_w_pool"] is not None
    assert [market.lower() for market in morpho.requested] == [WSTETH_WETH]


def test_borrower_graph_is_marked_not_applicable(morpho_client, clock):
    morpho = WithPositions(morpho_client, clock)
    service = build_service(morpho, clock, oracles=PricedOracles(clock, "2500"))

    response = analyze(service, borrower_request("0.15"))

    assert position_metrics(response)["solvency_model"] == "not_applicable"
    assert morpho.requested == []
    assert any("lender-only" in warning for warning in response.graph.completeness_warnings)
