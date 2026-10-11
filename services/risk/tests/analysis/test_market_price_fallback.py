"""`HybridProbabilityProvider`: Chainlink history first, market prices only as the depeg fallback."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analysis.graph_builder import build_risk_graph
from app.analysis.service import DepegEvent, HybridProbabilityProvider
from app.catalog.assets import load_asset_registry
from app.contracts.models import (
    CatalogEntityType,
    CatalogEntry,
    CatalogProvenance,
    ConfidenceLevel,
    FreshnessStatus,
    PositionInput,
    PositionMode,
    ProbabilityEstimate,
    ProbabilityMethod,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.graph import GraphContext, OracleObservation
from app.domain.position import validate_position
from app.domain.snapshots import AssetInfo, DataEnvelope, MarketSnapshot
from app.history.estimator import EmpiricalProbabilityEvidence


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
MARKET = "0x" + "ab" * 32
EURCV = "0x5F7827FDeb7c20b443265Fc2F40845B715385Ff2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
ORACLE = "0x00000000000000000000000000000000000000c1"
DEPEG = ScenarioSelection(scenario_id=ScenarioId.STABLECOIN_DEPEG, severity=Decimal("0.10"))
SHOCK = ScenarioSelection(scenario_id=ScenarioId.COLLATERAL_SHOCK, severity=Decimal("0.15"))


def lender_graph(loan: tuple[str, str, str], collateral: tuple[str, str, str]):
    def info(address, symbol, price):
        return AssetInfo(address=address, symbol=symbol, decimals=18, price_usd=Decimal(price))

    snapshot = MarketSnapshot(
        chain_id=1,
        market_id=MARKET,
        loan_asset=info(*loan),
        collateral_asset=info(*collateral),
        oracle_address=ORACLE,
        irm_address="0x000000000000000000000000000000000000cafe",
        lltv=Decimal("0.86"),
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
    entry = CatalogEntry(
        entity_id="synthetic",
        chain_id=1,
        entity_type=CatalogEntityType.MORPHO_MARKET,
        address_or_market_id=MARKET,
        name="synthetic",
        description="synthetic",
        provenance=CatalogProvenance.REPRESENTATIVE_PROXY,
        provenance_label="synthetic",
        supported=True,
        supported_modes=(PositionMode.LENDER,),
        lltv=None,
    )

    def envelope(value):
        return DataEnvelope(
            value=value,
            source="https://morpho.test/market",
            fetched_at=NOW,
            freshness=FreshnessStatus.FRESH,
            block_number=1,
        )

    position = validate_position(
        PositionInput(mode=PositionMode.LENDER, entity_id="synthetic", amount=Decimal("100"), numeraire=loan[1]),
        entry,
        snapshot,
        now=NOW,
    )
    context = GraphContext(
        entry=entry,
        market=envelope(snapshot),
        oracles={
            ORACLE: envelope(
                OracleObservation(oracle_address=ORACLE, age_seconds=Decimal(60), deviation=Decimal("0.001"))
            )
        },
        asset_registry=load_asset_registry(),
    )
    return build_risk_graph(position, context, clock=lambda: NOW)


def estimate(central: str | None, source: str, confidence=ConfidenceLevel.MEDIUM) -> ProbabilityEstimate:
    if central is None:
        return ProbabilityEstimate(
            method=ProbabilityMethod.UNAVAILABLE,
            low=None,
            central=None,
            high=None,
            confidence=ConfidenceLevel.UNAVAILABLE,
            horizon_days=30,
            source=source,
            retrieved_at=NOW,
            freshness=FreshnessStatus.EXPIRED,
            explanation="DeFiLlama coins chart for ethereum:0x5f78 unavailable: HTTP 500",
        )
    value = Decimal(central)
    return ProbabilityEstimate(
        method=ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=value / 2,
        central=value,
        high=value * 2,
        confidence=confidence,
        horizon_days=30,
        source=source,
        retrieved_at=NOW,
        freshness=FreshnessStatus.FRESH,
        explanation=f"{source} estimate",
    )


def evidence(probability: ProbabilityEstimate, start, end, event) -> EmpiricalProbabilityEvidence:
    return EmpiricalProbabilityEvidence(
        probability=probability,
        observation_count=0,
        expected_observation_count=0,
        window_count=0,
        event_window_count=0,
        sample_dates=(),
        gaps=(),
        missing_observation_ratio=Decimal(0),
        observation_period=(start, end),
        sampling_interval=timedelta(hours=1),
        threshold_definition=event.description,
    )


class FakeHistory:
    def __init__(self, centrals: dict[str, str]) -> None:
        self.centrals = centrals
        self.calls: list[str] = []

    def estimate(self, feed, start, end, event, *, now):
        self.calls.append(feed)
        return evidence(estimate(self.centrals.get(feed), f"chainlink:{feed}", ConfidenceLevel.HIGH), start, end, event)


class FakeMarketPrices:
    def __init__(self, central: str | None) -> None:
        self.central = central
        self.calls: list[tuple[str, str, datetime, datetime, object]] = []

    def estimate(self, address, symbol, start, end, event, *, now):
        self.calls.append((address, symbol, start, end, event))
        return evidence(estimate(self.central, "defillama-coins:test"), start, end, event)


EURCV_LOAN = lender_graph((EURCV, "EURCV", "1.17"), (WETH, "WETH", "2500"))


def test_eurcv_without_a_feed_uses_the_market_price_fallback():
    history = FakeHistory({})
    market = FakeMarketPrices("0.002")
    provider = HybridProbabilityProvider(history, None, market_prices=market)

    assessment = provider.assess(DEPEG, EURCV_LOAN, None, now=NOW)

    assert assessment.estimate.central == Decimal("0.002")
    assert assessment.estimate.source == "defillama-coins:test"
    assert assessment.estimate.confidence is ConfidenceLevel.MEDIUM
    assert history.calls == []
    ((address, symbol, start, end, event),) = market.calls
    assert (address, symbol) == (EURCV.lower(), "EURCV")
    assert end == NOW and end - start == timedelta(days=365)
    # The very event the Chainlink path uses, with the scenario severity.
    assert isinstance(event, DepegEvent)
    assert event.threshold == Decimal("0.90")


def test_a_chainlink_feed_is_used_before_the_fallback():
    graph = lender_graph((USDC, "USDC", "1"), (WETH, "WETH", "2500"))
    history = FakeHistory({"USDC/USD": "0.001"})
    market = FakeMarketPrices("0.5")
    provider = HybridProbabilityProvider(history, None, market_prices=market)

    assessment = provider.assess(DEPEG, graph, None, now=NOW)

    assert history.calls == ["USDC/USD"]
    assert market.calls == []
    assert assessment.estimate.central == Decimal("0.001")
    assert assessment.estimate.confidence is ConfidenceLevel.HIGH


def test_feed_and_fallback_assets_combine_conservatively():
    graph = lender_graph((USDC, "USDC", "1"), (EURCV, "EURCV", "1.17"))
    history = FakeHistory({"USDC/USD": "0.001"})
    market = FakeMarketPrices("0.004")
    provider = HybridProbabilityProvider(history, None, market_prices=market)

    assessment = provider.assess(DEPEG, graph, None, now=NOW)

    assert history.calls == ["USDC/USD"]
    assert [call[1] for call in market.calls] == ["EURCV"]
    assert assessment.estimate.central == Decimal("0.004")
    assert assessment.estimate.confidence is ConfidenceLevel.MEDIUM


def test_failed_fallback_keeps_the_unavailable_estimate_and_reason():
    provider = HybridProbabilityProvider(FakeHistory({}), None, market_prices=FakeMarketPrices(None))
    without = HybridProbabilityProvider(FakeHistory({}), None)

    failed = provider.assess(DEPEG, EURCV_LOAN, None, now=NOW).estimate
    unconfigured = without.assess(DEPEG, EURCV_LOAN, None, now=NOW).estimate

    reason = (
        "No price-history feed for EURCV; the stablecoin-depeg probability is unavailable "
        "rather than approximated by an unrelated feed."
    )
    assert unconfigured.explanation == reason
    assert unconfigured.method is ProbabilityMethod.UNAVAILABLE
    assert failed.method is ProbabilityMethod.UNAVAILABLE
    assert failed.central is None
    assert failed.source == unconfigured.source
    assert failed.explanation.startswith(reason)
    assert "Market-price fallback: DeFiLlama coins chart" in failed.explanation


def test_fallback_works_without_chainlink_history_configured():
    market = FakeMarketPrices("0.003")
    provider = HybridProbabilityProvider(None, None, market_prices=market)

    assert provider.assess(DEPEG, EURCV_LOAN, None, now=NOW).estimate.central == Decimal("0.003")


def test_collateral_shock_never_uses_the_market_price_fallback():
    market = FakeMarketPrices("0.5")
    provider = HybridProbabilityProvider(
        FakeHistory({}), None, collateral_feeds={}, market_prices=market
    )
    graph = lender_graph((USDC, "USDC", "1"), (WETH, "WETH", "2500"))

    assessment = provider.assess(SHOCK, graph, None, now=NOW)

    assert market.calls == []
    assert assessment.estimate.method is ProbabilityMethod.UNAVAILABLE
    assert "No price-history feed for WETH" in assessment.estimate.explanation


def test_default_wiring_configures_the_market_price_fallback(monkeypatch, tmp_path):
    from app.api.analysis import build_default_analysis_service
    from app.history.market_price import MarketPriceDepegHistory

    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)
    monkeypatch.setenv("RISK0_CACHE_PATH", str(tmp_path / "cache.sqlite3"))

    provider = build_default_analysis_service()._probabilities

    assert isinstance(provider._market_prices, MarketPriceDepegHistory)
