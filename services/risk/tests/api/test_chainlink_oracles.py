"""Live Chainlink oracle health, prices and oracle-failure probability over an in-memory chain.

The real `EthereumClient`, `MorphoOracleReader`, `ChainlinkLatest`,
`ChainlinkOracleHealth` and `HistoricalProbabilityService` run against
`FakeChain`, which answers eth_call and Multicall3 like mainnet contracts.
"""

import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from app.adapters.ethereum import EthereumClient
from app.catalog.repository import CatalogRepository
from app.analysis.service import (
    HISTORY_HEARTBEATS,
    ChainlinkLatest,
    ChainlinkOracleHealth,
    ChainlinkPrices,
    HybridProbabilityProvider,
)
from app.config import Settings
from app.contracts.models import (
    ConfidenceLevel,
    ProbabilityMethod,
    RecommendationDecision,
    RiskEdgeType,
    RiskNodeType,
)
from app.history.estimator import HistoricalProbabilityService
from app.history.normalization import NormalizedHistoryCache
from app.infrastructure.cache import SQLiteCache
from tests.api.test_analysis import (
    CATALOG_PATH,
    FakeHistory,
    FakeProbabilities,
    RecordedAllocationsOnly,
    analyze,
    borrower_request,
    build_service,
    lender_vault_request,
    service_logs,
)
from tests.fixtures.rpc.fake_chain import FakeChain, Feed


ETH_USD = "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419"
USDC_USD = "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6"
UNLISTED_ETH_USD = "0xc0053f3fbccd593758258334dfce24c2a9a673ad"
ADAPTER = "0x905b7dabcd3ce6b792d874e303d336424cdb1421"


def weth_usdc_market(morpho_client):
    entry = next(
        item for item in CatalogRepository(CATALOG_PATH).list_entries() if item.entity_id == "weth-usdc-860"
    )
    return morpho_client.get_market(entry.address_or_market_id).value


def ago(clock, seconds: float) -> int:
    return int((clock() - timedelta(seconds=seconds)).timestamp())


def oracle_price(market, factor: Decimal = Decimal(1)) -> int:
    ratio = market.collateral_asset.price_usd / market.loan_asset.price_usd * factor
    exponent = 36 + market.loan_asset.decimals - market.collateral_asset.decimals
    return int(ratio.scaleb(exponent))


def healthy_chain(market, clock, *, eth_age=600, eth_factor=Decimal(1), eth_feed=ETH_USD):
    chain = FakeChain()
    eth_answer = int((market.collateral_asset.price_usd * eth_factor).scaleb(8))
    chain.add_feed(eth_feed, Feed("ETH / USD", 8, {7: [(34503, eth_answer, ago(clock, eth_age))]}))
    usdc_answer = int(market.loan_asset.price_usd.scaleb(8))
    chain.add_feed(USDC_USD, Feed("USDC / USD", 8, {4: [(28, usdc_answer, ago(clock, 3 * 3600))]}))
    chain.add_oracle(
        market.oracle_address,
        base_feed_1=eth_feed,
        quote_feed_1=USDC_USD,
        price=oracle_price(market, eth_factor),
    )
    return chain


def client_for(chain, clock, **kwargs) -> EthereumClient:
    settings = Settings("https://rpc.example.invalid/v2/fake-rpc-credential")
    return EthereumClient(settings, rpc_call=chain.rpc, clock=clock, **kwargs)


def chainlink_service(morpho, clock, client, **kwargs):
    reader = ChainlinkLatest(client, clock=clock)
    return build_service(
        morpho,
        clock,
        oracles=ChainlinkOracleHealth(reader),
        prices=ChainlinkPrices(reader),
        **kwargs,
    )


def feed_nodes(graph):
    oracle_ids = {node.node_id for node in graph.nodes if node.node_type is RiskNodeType.ORACLE}
    children = {
        edge.target
        for edge in graph.edges
        if edge.relationship is RiskEdgeType.USES_ORACLE and edge.source in oracle_ids
    }
    return {node.node_id: node for node in graph.nodes if node.node_id in children}


# Live oracle health


def test_decoded_oracle_with_healthy_feeds_does_not_trigger(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock), clock)

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is RecommendationDecision.STAY
    assert response.recommendation.triggers == ()
    oracle = next(node for node in response.graph.nodes if node.metrics.get("decomposed") is True)
    assert oracle.metrics["oracle_type"] == "MorphoChainlinkOracleV2"
    assert oracle.metrics["age_seconds"] == Decimal(0)  # within the heartbeat
    assert oracle.metrics["deviation"] < Decimal("1e-18")


def test_feeds_appear_under_the_oracle_node_with_heartbeat_and_age(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock, eth_age=900), clock)

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    feeds = feed_nodes(response.graph)
    by_description = {node.metrics["description"]: node for node in feeds.values()}
    assert set(by_description) == {"ETH / USD", "USDC / USD"}
    eth = by_description["ETH / USD"]
    assert eth.node_type is RiskNodeType.ORACLE
    assert eth.metrics["feed_kind"] == "chainlink"
    assert eth.metrics["feed_role"] == "BASE_FEED_1"
    assert eth.metrics["heartbeat_seconds"] == Decimal(3600)
    assert eth.metrics["heartbeat_source"] == "documented (ETH/USD)"
    assert eth.metrics["last_update_age_seconds"] == Decimal(900)
    assert eth.metrics["age_seconds"] == Decimal(0)
    assert by_description["USDC / USD"].metrics["heartbeat_seconds"] == Decimal(86400)


def test_staleness_beyond_the_heartbeat_is_reported(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock, eth_age=3600 + 1799), clock)

    envelope = ChainlinkOracleHealth(ChainlinkLatest(client, clock=clock)).observe(market, now=clock())

    assert envelope is not None
    # ETH/USD heartbeat is 3600 s: 1799 s beyond it; USDC/USD is inside its 86400 s heartbeat.
    assert envelope.value.age_seconds == Decimal(1799)
    assert envelope.value.oracle_address == market.oracle_address
    assert "beyond" in envelope.source
    assert [feed.staleness_seconds for feed in envelope.value.feeds] == [Decimal(1799), Decimal(0)]


@pytest.mark.parametrize(
    ("eth_age", "decision"),
    [(3600 + 1799, RecommendationDecision.STAY), (3600 + 1800, RecommendationDecision.EXIT)],
    ids=["heartbeat+29m59s", "heartbeat+30m"],
)
def test_feed_stale_for_thirty_minutes_beyond_its_heartbeat_exits(
    morpho_client, clock, eth_age, decision
):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock, eth_age=eth_age), clock)

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is decision
    if decision is RecommendationDecision.EXIT:
        oracle = node_id_of_market_oracle(response.graph)
        assert response.recommendation.triggers == (f"live:oracle_failure:{oracle}",)


def node_id_of_market_oracle(graph):
    return next(node.node_id for node in graph.nodes if node.metrics.get("decomposed") is True)


@pytest.mark.parametrize(
    ("factor", "decision"),
    [("1.049", RecommendationDecision.STAY), ("1.05", RecommendationDecision.EXIT)],
    ids=["4.9%", "5%"],
)
def test_oracle_price_deviating_five_percent_from_the_reference_exits(
    morpho_client, clock, factor, decision
):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock, eth_factor=Decimal(factor)), clock)

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is decision


def test_provider_error_is_unavailable_and_sanitized(morpho_client, clock, caplog):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    chain.fail = True

    with caplog.at_level(logging.DEBUG):
        response = analyze(
            chainlink_service(morpho_client, clock, client_for(chain, clock)), borrower_request("0.05")
        )

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE
    assert "live oracle" in response.recommendation.explanation
    assert "rpc.example.invalid" not in service_logs(caplog)
    assert "fake-rpc-credential" not in response.model_dump_json()


def test_feed_without_a_round_is_unavailable(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {7: [(1, 1, 0)]}))  # updatedAt 0

    response = analyze(
        chainlink_service(morpho_client, clock, client_for(chain, clock)), borrower_request("0.05")
    )

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE


def test_oracle_that_is_not_decodable_is_unavailable_never_healthy(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    chain.handlers.pop(market.oracle_address.lower())  # not a Morpho Chainlink oracle

    response = analyze(
        chainlink_service(morpho_client, clock, client_for(chain, clock)), borrower_request("0.05")
    )

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE
    assert "live oracle" in response.recommendation.explanation


def test_rate_adapter_oracle_is_observed_without_staleness(morpho_client, clock):
    # The recorded MEV vault allocation is the wstETH/WETH market, priced by the
    # documented wstETH/stETH exchange-rate adapter.
    wsteth = morpho_client.get_market(
        "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e"
    ).value
    chain = FakeChain()
    chain.add_rate_adapter(ADAPTER, "wstETH/stETH exchange rate", 1246013674805236042)
    chain.add_oracle(wsteth.oracle_address, base_feed_1=ADAPTER, price=oracle_price(wsteth))
    weth = weth_usdc_market(morpho_client)
    chain.add_feed(
        ETH_USD,
        Feed("ETH / USD", 8, {7: [(1, int(weth.collateral_asset.price_usd.scaleb(8)), ago(clock, 60))]}),
    )
    chain.add_feed(USDC_USD, Feed("USDC / USD", 8, {4: [(1, 10**8, ago(clock, 60))]}))
    client = client_for(chain, clock)

    envelope = ChainlinkOracleHealth(ChainlinkLatest(client, clock=clock)).observe(wsteth, now=clock())
    response = analyze(
        chainlink_service(RecordedAllocationsOnly(morpho_client), clock, client),
        lender_vault_request(),
    )

    assert envelope is not None
    assert envelope.value.age_seconds == 0
    (feed,) = envelope.value.feeds
    assert feed.kind == "exchange_rate_adapter"
    assert feed.answer == Decimal("1.246013674805236042")
    assert response.recommendation.decision is not RecommendationDecision.UNAVAILABLE


def test_heartbeat_of_an_unlisted_feed_is_inferred_from_round_history(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock, eth_feed=UNLISTED_ETH_USD)
    # The oracle reads an ETH / USD proxy outside the catalog: replace it with an hourly history.
    rounds = [(k, 250000000000 + k, ago(clock, 3600 * (200 - k) + 600)) for k in range(1, 201)]
    chain.add_feed(UNLISTED_ETH_USD, Feed("ETH / USD", 8, {2: rounds}))
    client = client_for(chain, clock)

    envelope = ChainlinkOracleHealth(ChainlinkLatest(client, clock=clock)).observe(market, now=clock())

    assert envelope is not None
    eth = envelope.value.feeds[0]
    assert eth.heartbeat_seconds == Decimal(3600)
    assert eth.heartbeat_source.startswith("inferred (p99 of")


def test_oracle_decoding_is_memoized_across_analyses(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    service = chainlink_service(morpho_client, clock, client_for(chain, clock))

    analyze(service, borrower_request("0.05"))
    analyze(service, borrower_request("0.05"))

    oracle = market.oracle_address.lower()
    assert chain.calls.count((oracle, "0xf50a4718")) == 1  # BASE_FEED_1 read once
    assert chain.calls.count((oracle, "0xa035b1fe")) == 2  # price() per observation


# Oracle-failure probability


def staleness_service(morpho_client, clock, chain, tmp_path, *, lookback=timedelta(days=60)):
    client = client_for(chain, clock)
    history = HistoricalProbabilityService(
        client,
        NormalizedHistoryCache(SQLiteCache(tmp_path / "history.sqlite3")),
        heartbeats=HISTORY_HEARTBEATS,
    )
    provider = HybridProbabilityProvider(history, evidence=None, lookback=lookback)
    return chainlink_service(morpho_client, clock, client, probabilities=provider)


def oracle_failure_request():
    return borrower_request(scenarios=[{"scenario_id": "oracle-failure", "severity": "0.2"}])


def test_oracle_failure_probability_comes_from_exact_round_gaps(morpho_client, clock, tmp_path):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    eth_answer = int(market.collateral_asset.price_usd.scaleb(8))
    hours = 61 * 24
    # Hourly ETH/USD rounds with one 3-hour silence 20 days ago; daily USDC/USD rounds.
    silence = range(20 * 24, 20 * 24 + 3)
    eth_rounds = [
        (k, eth_answer, ago(clock, 3600 * (hours - k) + 600))
        for k in range(1, hours + 1)
        if hours - k not in silence
    ]
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {7: eth_rounds}))
    usdc = int(market.loan_asset.price_usd.scaleb(8))
    chain.add_feed(
        USDC_USD,
        Feed("USDC / USD", 8, {4: [(k, usdc, ago(clock, 86400 * (62 - k))) for k in range(1, 63)]}),
    )

    response = analyze(staleness_service(morpho_client, clock, chain, tmp_path), oracle_failure_request())

    (result,) = response.scenario_results
    probability = result.probability
    assert probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert Decimal(0) < probability.central < Decimal(1)
    assert probability.low <= probability.central <= probability.high
    assert "3600 s heartbeat + 1800 s" in probability.explanation
    assert "ETH / USD" in probability.explanation
    assert response.recommendation.decision is not RecommendationDecision.UNAVAILABLE


def test_oracle_failure_without_any_gap_is_zero_with_an_upper_bound(morpho_client, clock, tmp_path):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    hours = 61 * 24
    chain.add_feed(
        ETH_USD,
        Feed("ETH / USD", 8, {7: [(k, 1, ago(clock, 3600 * (hours - k) + 60)) for k in range(1, hours + 1)]}),
    )
    chain.add_feed(
        USDC_USD,
        Feed("USDC / USD", 8, {4: [(k, 10**8, ago(clock, 86400 * (62 - k) + 60)) for k in range(1, 63)]}),
    )

    response = analyze(staleness_service(morpho_client, clock, chain, tmp_path), oracle_failure_request())

    (result,) = response.scenario_results
    assert result.probability.central == 0
    assert result.probability.high > 0


def test_oracle_failure_is_unavailable_when_the_oracle_was_not_decoded(morpho_client, clock):
    provider = HybridProbabilityProvider(None, evidence=None)

    # The default fake oracle provider reports health without decoding the oracle.
    response = analyze(build_service(morpho_client, clock, probabilities=provider), oracle_failure_request())

    (result,) = response.scenario_results
    assert result.probability.method is ProbabilityMethod.UNAVAILABLE
    assert "not decoded" in result.probability.explanation


def test_oracle_failure_feeds_combine_conservatively_without_a_data_conflict(morpho_client, clock):
    class Staleness:
        def __init__(self):
            self.calls = []

        def estimate_staleness(self, address, heartbeat, start, end, *, now, label=None):
            self.calls.append((address, heartbeat))
            central = {ETH_USD: "0.02", USDC_USD: "0.05"}[address]
            return FakeHistory({"x": central}).estimate("x", start, end, lambda w: False, now=now)

    market = weth_usdc_market(morpho_client)
    history = Staleness()
    client = client_for(healthy_chain(market, clock), clock)
    service = chainlink_service(
        morpho_client, clock, client, probabilities=HybridProbabilityProvider(history, evidence=None)
    )

    response = analyze(service, oracle_failure_request())

    (result,) = response.scenario_results
    assert sorted(history.calls) == sorted([(ETH_USD, 3600), (USDC_USD, 86400)])
    assert result.probability.central == Decimal("0.05")
    assert result.probability.confidence is ConfidenceLevel.MEDIUM  # lowered: the feeds differ
    assert "conflict" not in response.recommendation.explanation.lower()


def test_adapter_only_oracles_have_no_staleness_probability(morpho_client, clock):
    wsteth = morpho_client.get_market(
        "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e"
    ).value
    chain = FakeChain()
    chain.add_rate_adapter(ADAPTER, "wstETH/stETH exchange rate", 1246013674805236042)
    chain.add_oracle(wsteth.oracle_address, base_feed_1=ADAPTER, price=oracle_price(wsteth))
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {7: [(1, 2500 * 10**8, ago(clock, 60))]}))
    chain.add_feed(USDC_USD, Feed("USDC / USD", 8, {4: [(1, 10**8, ago(clock, 60))]}))

    class NoCalls:
        def estimate_staleness(self, *args, **kwargs):
            raise AssertionError("an exchange-rate adapter has no update history")

    service = chainlink_service(
        RecordedAllocationsOnly(morpho_client),
        clock,
        client_for(chain, clock),
        probabilities=HybridProbabilityProvider(NoCalls(), evidence=None),
    )

    response = analyze(
        service, lender_vault_request(scenarios=[{"scenario_id": "oracle-failure", "severity": "0.2"}])
    )

    (result,) = response.scenario_results
    assert result.probability.central is None
    assert "exchange-rate adapters" in result.probability.explanation


def test_default_fake_probabilities_are_untouched_by_feed_nodes(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = client_for(healthy_chain(market, clock), clock)
    service = chainlink_service(
        morpho_client, clock, client, probabilities=FakeProbabilities({"oracle-failure": "0.001"})
    )

    response = analyze(service, oracle_failure_request())

    (result,) = response.scenario_results
    # Feed nodes sit on the oracle path and carry live age and deviation, so the
    # live check reports no missing data.
    assert "Live oracle age or deviation is unavailable." not in result.explanation
    assert any(node in result.affected_path for node in feed_nodes(response.graph))
