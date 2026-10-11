"""Non-Chainlink oracle feeds: classification in live health, unjudged staleness, priors.

The real `EthereumClient`, `MorphoOracleReader`, `ChainlinkOracleHealth` and the
analysis service run against `FakeChain` with Multicall3 block times enabled.
"""

from decimal import Decimal

import pytest

from app.adapters.morpho_oracle import PublishedHeartbeat, ThirdPartyFeedRegistry
from app.history.estimator import INSUFFICIENT_HISTORY_REASON
from app.analysis.service import (
    ChainlinkLatest,
    unavailable_estimate,
    ChainlinkOracleHealth,
    ChainlinkPrices,
    HybridProbabilityProvider,
)
from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
    RecommendationDecision,
)
from tests.api.test_analysis import FakeHistory, FakeProbabilities, analyze, borrower_request, build_service
from tests.api.test_chainlink_oracles import (
    ETH_USD,
    USDC_USD,
    ago,
    client_for,
    feed_nodes,
    healthy_chain,
    oracle_price,
    weth_usdc_market,
)
from tests.fixtures.rpc.fake_chain import FakeChain, Feed


NAV = "0x8b7b1689396ef4468df1c0fab181d29b47089609"
NAV_DESCRIPTION = "mGLOeuro/EUR"
REDSTONE = "0x7fda577c085330e69df3179a2a5867751a9d0caa"
REDSTONE_DESCRIPTION = "RedStone Price Feed for strUSD_FUNDAMENTAL"
DUMMY = "0xc3866d726c204c0836e0677a31973c649888973d"
OJO = "0x77ee147391eac11c92817e771860ff4aad265d89"
EIGHTEEN_DAYS = 18 * 86400


def third_party_chain(market, clock, *, base=NAV, description=NAV_DESCRIPTION, age=EIGHTEEN_DAYS,
                      factor=Decimal(1), rounds=None, history=False):
    chain = FakeChain()
    chain.block_timestamp = int(clock().timestamp())
    chain.add_third_party_feed(
        base, description, 8, rounds or [(2, 10**13, ago(clock, age))], history=history
    )
    usdc = int(market.loan_asset.price_usd.scaleb(8))
    chain.add_feed(USDC_USD, Feed("USDC / USD", 8, {4: [(28, usdc, ago(clock, 3600))]}))
    chain.add_oracle(
        market.oracle_address, base_feed_1=base, quote_feed_1=USDC_USD, price=oracle_price(market, factor)
    )
    return chain


def health(chain, clock, registry=None):
    reader = ChainlinkLatest(client_for(chain, clock), clock=clock)
    return reader, ChainlinkOracleHealth(reader, third_party=registry or ThirdPartyFeedRegistry())


def service_for(morpho, clock, chain, *, registry=None, probabilities=None):
    reader, oracles = health(chain, clock, registry)
    return build_service(
        morpho, clock, oracles=oracles, prices=ChainlinkPrices(reader), probabilities=probabilities
    )


def by_description(graph):
    return {node.metrics["description"]: node for node in feed_nodes(graph).values()}


# Live health: observed, staleness not judged


def test_third_party_feed_without_a_heartbeat_is_observed_not_missing(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock)

    response = analyze(service_for(morpho_client, clock, chain), borrower_request("0.05"))

    recommendation = response.recommendation
    assert recommendation.decision is RecommendationDecision.STAY
    assert recommendation.triggers == ()  # 18 days old, but no guessed threshold fires
    assert recommendation.confidence is ConfidenceLevel.LOW
    assert "staleness is not judged for mGLOeuro/EUR" in recommendation.explanation
    nav = by_description(response.graph)[NAV_DESCRIPTION]
    assert nav.metrics["feed_kind"] == "third_party"
    assert nav.metrics["provider"] == "Midas"
    assert nav.metrics["last_update_age_seconds"] == Decimal(EIGHTEEN_DAYS)
    assert nav.metrics["age_seconds"] is None
    assert nav.metrics["heartbeat_seconds"] is None
    assert nav.metrics["heartbeat_source"].startswith("unknown")
    assert nav.metrics["staleness_judged"] is False
    assert (
        "Midas feed mGLOeuro/EUR has no published heartbeat; last update 18.0 days ago; "
        "staleness not judged."
    ) in response.graph.completeness_warnings


def test_unjudged_staleness_never_fires_the_live_oracle_trigger(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock, age=400 * 86400)
    _, oracles = health(chain, clock)

    envelope = oracles.observe(market, now=clock())

    assert envelope is not None
    # Only the Chainlink USDC/USD feed is judged (inside its 24 h heartbeat).
    assert envelope.value.age_seconds == 0
    assert [feed.description for feed in envelope.value.unjudged_feeds] == [NAV_DESCRIPTION]
    assert "not judged" in envelope.source


def test_oracle_whose_only_feed_is_unjudged_has_unknown_age(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = FakeChain()
    chain.block_timestamp = int(clock().timestamp())
    chain.add_third_party_feed(NAV, NAV_DESCRIPTION, 8, [(2, 10**13, ago(clock, 60))])
    chain.add_oracle(market.oracle_address, base_feed_1=NAV, price=oracle_price(market))

    envelope = health(chain, clock)[1].observe(market, now=clock())

    assert envelope is not None
    assert envelope.value.age_seconds is None
    assert envelope.value.deviation is not None


@pytest.mark.parametrize(
    ("factor", "decision"),
    [("1.049", RecommendationDecision.STAY), ("1.05", RecommendationDecision.EXIT)],
    ids=["4.9%", "5%"],
)
def test_deviation_is_still_judged_for_an_unjudged_feed(morpho_client, clock, factor, decision):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock, factor=Decimal(factor))

    response = analyze(service_for(morpho_client, clock, chain), borrower_request("0.05"))

    assert response.recommendation.decision is decision
    if decision is RecommendationDecision.EXIT:
        assert response.recommendation.triggers[0].startswith("live:oracle_failure:")
        assert response.recommendation.confidence is ConfidenceLevel.LOW


def registry_for(address, description, seconds=21600):
    return ThirdPartyFeedRegistry(
        {
            address: PublishedHeartbeat(
                address=address,
                description=description,
                provider="RedStone",
                heartbeat_seconds=seconds,
                source_url="https://example.org/manifest.json",
            )
        }
    )


@pytest.mark.parametrize(
    ("age", "decision"),
    [(21600 + 1799, RecommendationDecision.STAY), (21600 + 1800, RecommendationDecision.EXIT)],
)
def test_published_heartbeat_is_judged_like_a_documented_one(morpho_client, clock, age, decision):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock, base=REDSTONE, description=REDSTONE_DESCRIPTION, age=age)
    registry = registry_for(REDSTONE, REDSTONE_DESCRIPTION)

    response = analyze(service_for(morpho_client, clock, chain, registry=registry), borrower_request("0.05"))

    assert response.recommendation.decision is decision
    feed = by_description(response.graph)[REDSTONE_DESCRIPTION]
    assert feed.metrics["heartbeat_seconds"] == Decimal(21600)
    assert feed.metrics["heartbeat_source"] == "published by RedStone (https://example.org/manifest.json)"
    assert feed.metrics["age_seconds"] == Decimal(max(0, age - 21600))
    assert not any("no published heartbeat" in item for item in response.graph.completeness_warnings)
    assert response.recommendation.confidence is not ConfidenceLevel.UNAVAILABLE
    assert "not judged" not in response.recommendation.explanation


def test_registry_entry_with_another_description_is_ignored(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock, base=REDSTONE, description="RedStone Price Feed for other")
    registry = registry_for(REDSTONE, REDSTONE_DESCRIPTION)

    envelope = health(chain, clock, registry)[1].observe(market, now=clock())

    assert envelope.value.feeds[0].heartbeat_seconds is None


def test_heartbeat_is_inferred_from_readable_third_party_rounds(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    rounds = [(k, 10**8, ago(clock, 3600 * (200 - k) + 600)) for k in range(1, 201)]
    chain = third_party_chain(
        market, clock, base=REDSTONE, description=REDSTONE_DESCRIPTION, rounds=rounds, history=True
    )

    envelope = health(chain, clock)[1].observe(market, now=clock())

    feed = envelope.value.feeds[0]
    assert feed.history_readable is True
    # Hourly cadence with one missed cycle tolerated, as for an unlisted Chainlink feed.
    assert feed.heartbeat_seconds == Decimal(7200)
    assert feed.heartbeat_source.startswith("inferred")


def test_too_little_round_history_leaves_the_heartbeat_unknown(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    rounds = [(1, 10**13, ago(clock, 86400 * 67)), (2, 10**13, ago(clock, EIGHTEEN_DAYS))]
    chain = third_party_chain(market, clock, rounds=rounds, history=True)

    envelope = health(chain, clock)[1].observe(market, now=clock())

    feed = envelope.value.feeds[0]
    assert feed.history_readable is True
    assert feed.heartbeat_seconds is None and feed.staleness_seconds is None


def test_computed_and_constant_feeds_cannot_be_stale(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = FakeChain()
    chain.block_timestamp = int(clock().timestamp())
    chain.add_computed_feed(OJO, "Ojo PT Feed Pendle Chainlink-compatible Oracle", 981859051090815957)
    chain.add_computed_feed(
        DUMMY, "Dummy feed with 12 decimals", 10**12, decimals=12, round_id=1, any_round=True
    )
    chain.add_oracle(
        market.oracle_address, base_feed_1=OJO, quote_feed_1=DUMMY, price=oracle_price(market)
    )

    envelope = health(chain, clock)[1].observe(market, now=clock())

    assert envelope is not None
    assert [feed.kind for feed in envelope.value.feeds] == ["computed_on_read", "constant_scaler"]
    assert [feed.staleness_seconds for feed in envelope.value.feeds] == [0, 0]
    assert envelope.value.age_seconds == 0
    assert envelope.value.unjudged_feeds == ()


def test_constant_scaler_that_changes_its_answer_is_unobservable(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = FakeChain()
    chain.block_timestamp = int(clock().timestamp())
    state = chain.add_computed_feed(
        DUMMY, "Dummy feed with 12 decimals", 10**12, decimals=12, round_id=1, any_round=True
    )
    chain.add_oracle(market.oracle_address, base_feed_1=DUMMY, price=oracle_price(market))
    _, oracles = health(chain, clock)
    assert oracles.observe(market, now=clock()) is not None

    state["answer"] = 2 * 10**12

    assert oracles.observe(market, now=clock()) is None


# Oracle-failure probability


def prior_estimate(now, central="0.004"):
    return ProbabilityEstimate(
        method=ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL,
        low=Decimal("0.001"),
        central=Decimal(central),
        high=Decimal("0.01"),
        confidence=ConfidenceLevel.LOW,
        horizon_days=30,
        source="defillama:/hacks; defillama:/protocols",
        retrieved_at=now,
        freshness=FreshnessStatus.FRESH,
        explanation="Industry oracle-exploit prior (oracle-exploit-prior-v1) from DeFiLlama.",
    )


class PriorEvidence:
    def __init__(self, central="0.004"):
        self.central = central
        self.calls = 0

    def oracle_exploit_base_rate(self, *, now):
        self.calls += 1
        return prior_estimate(now, self.central)


class Staleness:
    def __init__(self, centrals):
        self.centrals = centrals
        self.calls = []

    def estimate_staleness(self, address, heartbeat, start, end, *, now, label=None, require_phase=True):
        self.calls.append((address, heartbeat, require_phase))
        return FakeHistory({"x": self.centrals[address]}).estimate("x", start, end, lambda w: False, now=now)


class OracleOnly:
    """Hybrid provider for oracle-failure; fixed probabilities for every other scenario."""

    def __init__(self, hybrid):
        self.hybrid = hybrid
        self.fake = FakeProbabilities({"collateral-shock": "0.001"})

    def assess(self, selection, graph, position, *, now):
        if selection.scenario_id.value == "oracle-failure":
            return self.hybrid.assess(selection, graph, position, now=now)
        return self.fake.assess(selection, graph, position, now=now)


def oracle_failure_request():
    return borrower_request(scenarios=[{"scenario_id": "oracle-failure", "severity": "0.2"}])


def oracle_failure(morpho, clock, chain, hybrid, registry=None):
    response = analyze(
        service_for(morpho, clock, chain, registry=registry, probabilities=OracleOnly(hybrid)),
        oracle_failure_request(),
    )
    (result,) = response.scenario_results
    return response, result.probability


def test_third_party_feed_without_history_uses_the_explained_industry_prior(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock)
    history = Staleness({USDC_USD: "0.001"})
    evidence = PriorEvidence("0.004")

    response, probability = oracle_failure(
        morpho_client, clock, chain, HybridProbabilityProvider(history, evidence)
    )

    assert probability.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert probability.central == Decimal("0.004")  # the higher of prior and USDC/USD staleness
    assert probability.confidence is ConfidenceLevel.LOW
    assert "Industry prior, not a feed-specific estimate" in probability.explanation
    assert f"mGLOeuro/EUR ({NAV})" in probability.explanation
    assert history.calls == [(USDC_USD, 86400, True)]
    assert response.recommendation.decision is not RecommendationDecision.UNAVAILABLE


def test_third_party_feed_with_history_and_heartbeat_uses_empirical_staleness(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    rounds = [(k, 10**8, ago(clock, 3600 * (200 - k) + 600)) for k in range(1, 201)]
    chain = third_party_chain(
        market, clock, base=REDSTONE, description=REDSTONE_DESCRIPTION, rounds=rounds, history=True
    )
    history = Staleness({USDC_USD: "0.001", REDSTONE: "0.002"})
    evidence = PriorEvidence()

    _, probability = oracle_failure(
        morpho_client, clock, chain, HybridProbabilityProvider(history, evidence),
        registry=registry_for(REDSTONE, REDSTONE_DESCRIPTION),
    )

    assert probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert probability.central == Decimal("0.002")
    assert (REDSTONE, 21600, False) in history.calls
    assert evidence.calls == 0


def test_published_heartbeat_without_readable_history_uses_the_prior(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock, base=REDSTONE, description=REDSTONE_DESCRIPTION, age=60)
    history = Staleness({USDC_USD: "0.001"})

    _, probability = oracle_failure(
        morpho_client, clock, chain, HybridProbabilityProvider(history, PriorEvidence()),
        registry=registry_for(REDSTONE, REDSTONE_DESCRIPTION),
    )

    assert probability.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert "no readable round history" in probability.explanation


def test_missing_prior_makes_the_probability_unavailable(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = third_party_chain(market, clock)

    _, probability = oracle_failure(
        morpho_client, clock, chain, HybridProbabilityProvider(Staleness({USDC_USD: "0.001"}), None)
    )

    assert probability.method is ProbabilityMethod.UNAVAILABLE
    assert "prior" in probability.explanation


def test_computed_and_constant_feeds_are_a_structural_zero(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = FakeChain()
    chain.block_timestamp = int(clock().timestamp())
    chain.add_computed_feed(OJO, "Ojo PT Feed Pendle Chainlink-compatible Oracle", 981859051090815957)
    chain.add_computed_feed(
        DUMMY, "Dummy feed with 12 decimals", 10**12, decimals=12, round_id=1, any_round=True
    )
    chain.add_oracle(market.oracle_address, base_feed_1=OJO, quote_feed_1=DUMMY, price=oracle_price(market))

    class NoCalls:
        def estimate_staleness(self, *args, **kwargs):
            raise AssertionError("a feed computed when read has no update history")

    _, probability = oracle_failure(
        morpho_client, clock, chain, HybridProbabilityProvider(NoCalls(), PriorEvidence())
    )

    assert probability.method is ProbabilityMethod.STRUCTURAL
    assert probability.central == 0
    assert "computed-on-read feeds, constant scalers" in probability.explanation


class ShortHistory:
    """ETH/USD staleness history is unusable (with `reason`); USDC/USD is fine."""

    def __init__(self, reason):
        self.reason = reason

    def estimate_staleness(self, address, heartbeat, start, end, *, now, label=None, require_phase=True):
        if address == ETH_USD:
            class Evidence:
                probability = unavailable_estimate(now, self.reason)

            return Evidence()
        return FakeHistory({"x": "0.001"}).estimate("x", start, end, lambda w: False, now=now)


def test_chainlink_feed_with_too_short_a_history_uses_the_industry_prior(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    hybrid = HybridProbabilityProvider(ShortHistory(INSUFFICIENT_HISTORY_REASON), PriorEvidence("0.004"))

    response, probability = oracle_failure(morpho_client, clock, chain, hybrid)

    # A feed launched under 30 days ago has no window yet: the explained prior
    # stands in, as for a third-party feed without history.
    assert probability.central == Decimal("0.004")
    assert probability.confidence is ConfidenceLevel.LOW
    assert "without a usable history" in probability.explanation
    assert response.recommendation.decision is not RecommendationDecision.UNAVAILABLE


def test_unreadable_chainlink_history_still_blocks_rather_than_using_the_prior(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    chain = healthy_chain(market, clock)
    hybrid = HybridProbabilityProvider(
        ShortHistory("Ethereum provider history is unavailable"), PriorEvidence("0.004")
    )

    _, probability = oracle_failure(morpho_client, clock, chain, hybrid)

    assert probability.central is None
