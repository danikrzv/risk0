import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.analysis.service import (
    AnalysisService,
    HybridProbabilityProvider,
    ProbabilityAssessment,
    combine_conservative,
)
from app.catalog.repository import CatalogRepository
from app.contracts.models import (
    AnalysisRequest,
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
    RecommendationDecision,
    ScenarioId,
    ScenarioSelection,
)
from app.domain.exit import ExitQuote, ExitRoute, QuoteRequest
from app.domain.graph import OracleObservation
from app.domain.snapshots import DataEnvelope
from app.history.estimator import EmpiricalProbabilityEvidence
from app.history.normalization import Observation
from app.main import app


ROOT = Path(__file__).resolve().parents[4]
CATALOG_PATH = ROOT / "data" / "catalog" / "ethereum.json"
RESPONSE_SCHEMA = json.loads(
    (ROOT / "packages" / "contracts" / "schemas" / "analysis-response.schema.json").read_text()
)
SNAPSHOT_BLOCK = 26161049
GAS_PRICE_WEI = Decimal("20000000000")
ETH_USD = Decimal("2492.8486759318967")
WSTETH_WETH = "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e"
STAY = RecommendationDecision.STAY.value
EXIT = RecommendationDecision.EXIT.value
UNAVAILABLE = RecommendationDecision.UNAVAILABLE.value


# Fakes: every provider is injected, so no test touches the network.


def estimate(central: str, *, source: str = "chainlink:ETH/USD", confidence=ConfidenceLevel.HIGH, now=None):
    value = Decimal(central)
    return ProbabilityEstimate(
        method=ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=value / 2,
        central=value,
        high=min(Decimal(1), value * 2),
        confidence=confidence,
        horizon_days=30,
        source=source,
        retrieved_at=now or datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        freshness=FreshnessStatus.FRESH,
        explanation="fixture estimate",
    )


class FakeProbabilities:
    def __init__(self, centrals: dict[str, str]) -> None:
        self.centrals = centrals

    def assess(self, selection, graph, position, *, now):
        # A "<scenario-id>@<severity>" key takes precedence over the bare scenario id,
        # so tests can give each severity its own probability, as the empirical
        # provider does.
        key = selection.scenario_id.value
        central = self.centrals.get(f"{key}@{selection.severity}", self.centrals.get(key))
        if central is None:
            return ProbabilityAssessment(
                estimate=ProbabilityEstimate(
                    method=ProbabilityMethod.UNAVAILABLE,
                    low=None,
                    central=None,
                    high=None,
                    confidence=ConfidenceLevel.UNAVAILABLE,
                    horizon_days=30,
                    source="fixture",
                    retrieved_at=now,
                    freshness=FreshnessStatus.EXPIRED,
                    explanation="no fixture probability",
                )
            )
        return ProbabilityAssessment(estimate=estimate(central, now=now))


class FakeQuotes:
    """Simulates QuoterV2: 0.05% fee, 0.1% price impact, 0.5% slippage buffer."""

    def __init__(self, clock, *, ages=(0,), blocks=(SNAPSHOT_BLOCK,), fail=False):
        self.clock = clock
        self.ages = list(ages)
        self.blocks = list(blocks)
        self.fail = fail
        self.calls: list[bool] = []

    def quote(self, request: QuoteRequest, *, refresh: bool) -> DataEnvelope[ExitQuote]:
        self.calls.append(refresh)
        if self.fail:
            raise RuntimeError("provider said https://rpc.example.invalid/v2/fake-rpc-credential failed")
        index = min(len(self.calls) - 1, len(self.ages) - 1)
        age = self.ages[index]
        block = self.blocks[min(len(self.calls) - 1, len(self.blocks) - 1)]
        quoted_at = self.clock() - timedelta(seconds=age)
        token_in, token_out = request.token_in, request.token_out
        marginal = (request.amount_in * token_in.price_usd / token_out.price_usd).quantize(
            Decimal(1).scaleb(-token_out.decimals)
        )
        fee = (marginal * Decimal("0.0005")).quantize(Decimal(1).scaleb(-token_out.decimals))
        impact = (marginal * Decimal("0.001")).quantize(Decimal(1).scaleb(-token_out.decimals))
        out = marginal - fee - impact
        minimum = (out * Decimal("0.995")).quantize(Decimal(1).scaleb(-token_out.decimals))
        quote = ExitQuote(
            request=request,
            route=ExitRoute(
                path=(token_in.address, token_out.address),
                fee_tiers=(500,),
                symbols=(token_in.symbol, token_out.symbol),
            ),
            amount_in=request.amount_in,
            amount_out=out,
            minimum_amount_out=minimum,
            marginal_amount_out=marginal,
            fee_fraction=Decimal("0.0005"),
            fee_cost_out=fee,
            price_impact=Decimal("0.001"),
            price_impact_cost_out=impact,
            gas_estimate=120_000,
            gas_price_wei=GAS_PRICE_WEI,
            gas_cost_eth=Decimal("0.0024"),
            gas_cost_out=None,
            net_amount_out=out,
            block_number=block,
            quoted_at=quoted_at,
            candidates=1,
            reverted=0,
        )
        return DataEnvelope[ExitQuote](
            value=quote,
            source="uniswap-v3:quoter-v2",
            fetched_at=quoted_at,
            freshness=FreshnessStatus.FRESH,
            block_number=block,
        )


class FakeGas:
    def gas_price_wei(self) -> Decimal:
        return GAS_PRICE_WEI


class FakePrices:
    def price_usd(self, symbol: str) -> Decimal | None:
        return {"WETH": ETH_USD, "ETH": ETH_USD, "USDC": Decimal("0.9997496425860051")}.get(
            symbol.upper()
        )


class FakeOracles:
    def __init__(self, clock, *, age="60", deviation="0.001", freshness=FreshnessStatus.FRESH):
        self.clock = clock
        self.age = Decimal(age)
        self.deviation = Decimal(deviation)
        self.freshness = freshness

    def observe(self, market, *, now):
        return DataEnvelope[OracleObservation](
            value=OracleObservation(
                oracle_address=market.oracle_address,
                age_seconds=self.age,
                deviation=self.deviation,
            ),
            source="fixture:oracle-health",
            fetched_at=now,
            freshness=self.freshness,
        )


class FakeIncidents:
    def __init__(self, incidents=()):
        self.incidents = tuple(incidents)

    def active_incidents(self, protocol: str) -> tuple[str, ...]:
        return self.incidents


class RecordedAllocationsOnly:
    """Morpho client wrapper that keeps only allocations whose market is recorded."""

    def __init__(self, client, keep=(WSTETH_WETH,)) -> None:
        self.client = client
        self.keep = {market.lower() for market in keep}

    def get_market(self, market_id):
        return self.client.get_market(market_id)

    def get_vault(self, address, version):
        return self.client.get_vault(address, version)

    def get_allocations(self, address, version):
        envelope = self.client.get_allocations(address, version)
        rows = tuple(row for row in envelope.value if (row.market_id or "").lower() in self.keep)
        return envelope.model_copy(update={"value": rows})


HEALTHY_ORACLES = object()


def build_service(
    morpho,
    clock,
    *,
    probabilities=None,
    quotes=None,
    oracles=HEALTHY_ORACLES,
    incidents=None,
    catalog_path=CATALOG_PATH,
    prices=None,
):
    """Defaults to healthy live oracle data; pass `oracles=None` for no oracle provider."""
    if oracles is HEALTHY_ORACLES:
        oracles = FakeOracles(clock)
    return AnalysisService(
        CatalogRepository(catalog_path),
        morpho,
        probabilities=probabilities
        or FakeProbabilities(
            {"collateral-shock": "0.001", "protocol-impairment": "0.0004", "stablecoin-depeg": "0.0001"}
        ),
        quotes=quotes if quotes is not None else FakeQuotes(clock),
        gas=FakeGas(),
        prices=prices if prices is not None else FakePrices(),
        oracles=oracles,
        incidents=incidents or FakeIncidents(),
        clock=clock,
    )


@pytest.fixture
def api():
    from app.api.analysis import get_analysis_service

    def use(service):
        app.dependency_overrides[get_analysis_service] = lambda: service
        return TestClient(app, raise_server_exceptions=False)

    yield use
    app.dependency_overrides.clear()


def lender_vault_request(numeraire="WETH", amount="10", scenarios=None):
    return {
        "position": {
            "mode": "lender",
            "entity_id": "mev-capital-weth",
            "amount": amount,
            "numeraire": numeraire,
        },
        "scenarios": scenarios
        or [
            {"scenario_id": "collateral-shock", "severity": "0.15"},
            {"scenario_id": "protocol-impairment", "severity": "0.10"},
        ],
    }


def borrower_request(severity="0.15", scenarios=None):
    return {
        "position": {
            "mode": "borrower",
            "entity_id": "weth-usdc-860",
            "amount": "19900",
            "collateral_amount": "10",
            "debt_amount": "19900",
            "ltv": "0.80",
            "numeraire": "USDC",
        },
        "scenarios": scenarios or [{"scenario_id": "collateral-shock", "severity": severity}],
    }


def assert_contract(body):
    Draft202012Validator(RESPONSE_SCHEMA).validate(body)
    # Shape the web proxy (apps/web/src/app/api/analysis/route.ts) requires.
    assert isinstance(body["graph"], dict)
    assert isinstance(body["recommendation"], dict)
    assert isinstance(body["exit_estimate"], dict)
    assert isinstance(body["scenario_results"], list)


def service_logs(caplog) -> str:
    # The TestClient's own httpx request log names the test server; check ours.
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("risk0")
    )


def post(client, payload):
    return client.post("/v1/analysis", json=payload)


# API: golden flows


def test_lender_vault_analysis_stays_and_matches_the_contract(api, morpho_client, clock):
    client = api(build_service(RecordedAllocationsOnly(morpho_client), clock))

    response = post(client, lender_vault_request())

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    recommendation = body["recommendation"]
    assert recommendation["decision"] == STAY
    # 10 WETH * 0.998207... allocation * 0.15 shock * 0.001 + 10 * 0.10 * 0.0004
    assert Decimal(recommendation["expected_loss"]) < Decimal(recommendation["exit_cost"])
    assert Decimal(recommendation["exit_cost"]) == Decimal("0.003")  # 150k gas at 20 gwei, in WETH
    assert body["exit_estimate"]["withdrawable"] is True
    assert recommendation["triggers"] == []
    assert recommendation["dominant_dependency"].startswith("asset:1:")
    assert {result["scenario_id"] for result in body["scenario_results"]} == {
        "collateral-shock",
        "protocol-impairment",
    }
    node_types = {node["node_type"] for node in body["graph"]["nodes"]}
    assert {"position", "vault", "market", "asset", "oracle", "protocol"} <= node_types


def test_borrower_fifteen_percent_shock_exits_on_liquidation(api, morpho_client, clock):
    client = api(build_service(morpho_client, clock))

    response = post(client, borrower_request("0.15"))

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    recommendation = body["recommendation"]
    assert recommendation["decision"] == EXIT
    assert "collateral-shock:liquidation" in recommendation["triggers"]
    (result,) = body["scenario_results"]
    assert result["hard_triggers"] == ["liquidation"]
    assert Decimal(result["stressed_values"]["stressed_ltv"]) >= Decimal("0.86")
    assert body["exit_estimate"]["route"] == ["WETH", "USDC@0.05%"]


def test_unknown_entity_is_rejected_with_422(api, morpho_client, clock, morpho_api):
    client = api(build_service(morpho_client, clock))
    payload = lender_vault_request()
    payload["position"]["entity_id"] = "not-in-catalog"

    response = post(client, payload)

    assert response.status_code == 422
    assert "catalog" in response.json()["detail"].lower()
    assert morpho_api.requests == []


def test_inactive_entry_is_rejected_with_422_without_calling_providers(
    api, morpho_client, clock, morpho_api, tmp_path
):
    catalog = json.loads(CATALOG_PATH.read_text())
    for entry in catalog["entries"]:
        if entry["entity_id"] == "weth-usdc-860":
            entry["supported"] = False
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    client = api(build_service(morpho_client, clock, catalog_path=path))

    response = post(client, borrower_request())

    assert response.status_code == 422
    assert "disabled" in response.json()["detail"].lower()
    assert morpho_api.requests == []


def test_invalid_position_is_rejected_with_a_sanitized_422(api, morpho_client, clock):
    client = api(build_service(morpho_client, clock))
    payload = borrower_request()
    payload["position"]["ltv"] = "0.90"  # above the 0.86 LLTV

    response = post(client, payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "ltv" in detail.lower()
    assert "Traceback" not in response.text


def test_out_of_range_severity_is_rejected_with_422(api, morpho_client, clock, morpho_api):
    client = api(build_service(morpho_client, clock))

    response = post(client, borrower_request("1.5"))

    assert response.status_code == 422
    assert morpho_api.requests == []


def test_morpho_outage_returns_a_sanitized_503(api, morpho_client, clock, morpho_api, caplog):
    morpho_api.mode = "down"
    client = api(build_service(morpho_client, clock))

    with caplog.at_level(logging.DEBUG):
        response = post(client, borrower_request())

    assert response.status_code == 503
    text = response.text
    assert "unavailable" in response.json()["detail"].lower()
    for leaked in ("api.morpho.org", "simulated outage", "Traceback", "http"):
        assert leaked not in text
        assert leaked not in service_logs(caplog)


def test_quote_provider_outage_is_unavailable_and_sanitized(api, morpho_client, clock, caplog):
    client = api(build_service(morpho_client, clock, quotes=FakeQuotes(clock, fail=True)))

    with caplog.at_level(logging.DEBUG):
        response = post(client, borrower_request("0.05"))

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    assert body["recommendation"]["decision"] == UNAVAILABLE
    assert body["exit_estimate"]["freshness"] == "expired"
    # Public data provenance (Morpho API paths) is expected; provider error text is not.
    for leaked in ("rpc.example.invalid", "fake-rpc-credential", "failed"):
        assert leaked not in response.text
        assert leaked not in service_logs(caplog)


def test_unmodelled_vault_allocations_make_the_analysis_unavailable(api, morpho_client, clock):
    # The recorded MEV vault allocates to markets whose snapshots are not available.
    client = api(build_service(morpho_client, clock))

    response = post(client, lender_vault_request())

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    assert body["recommendation"]["decision"] == UNAVAILABLE
    shock = next(r for r in body["scenario_results"] if r["scenario_id"] == "collateral-shock")
    assert shock["confidence"] == "unavailable"
    assert shock["stressed_values"]["impact_complete"] is False
    unknown = [n for n in body["graph"]["nodes"] if n["metrics"].get("exposure_unknown") is True]
    assert unknown


def test_live_oracle_breach_exits_even_without_an_oracle_scenario(api, morpho_client, clock):
    service = build_service(morpho_client, clock, oracles=FakeOracles(clock, age="3600"))
    client = api(service)

    response = post(client, borrower_request("0.05"))

    body = response.json()
    recommendation = body["recommendation"]
    assert recommendation["decision"] == EXIT
    (trigger,) = recommendation["triggers"]
    oracle = next(node for node in body["graph"]["nodes"] if node["node_type"] == "oracle")
    # The live trigger names the breached oracle, which is the dominant dependency.
    assert trigger == f"live:oracle_failure:{oracle['node_id']}"
    assert recommendation["dominant_dependency"] == oracle["node_id"]


def test_expired_live_oracle_data_is_unavailable(api, morpho_client, clock):
    oracles = FakeOracles(clock, freshness=FreshnessStatus.EXPIRED)
    client = api(build_service(morpho_client, clock, oracles=oracles))

    response = post(client, borrower_request("0.05"))

    body = response.json()
    assert_contract(body)
    assert body["recommendation"]["decision"] == UNAVAILABLE
    assert "oracle" in body["recommendation"]["explanation"].lower()


def test_protocol_emergency_exits(api, morpho_client, clock):
    service = build_service(
        morpho_client, clock, incidents=FakeIncidents(("Morpho paused markets",))
    )

    body = post(api(service), borrower_request("0.05")).json()

    assert body["recommendation"]["decision"] == EXIT
    assert body["recommendation"]["triggers"] == ["live:protocol_emergency:protocol:1:morpho"]
    assert body["recommendation"]["dominant_dependency"] == "protocol:1:morpho"


def test_live_oracle_breach_folds_the_live_data_confidence_in(api, morpho_client, clock):
    oracles = FakeOracles(clock, deviation="0.2", freshness=FreshnessStatus.STALE_USABLE)
    body = post(api(build_service(morpho_client, clock, oracles=oracles)), borrower_request("0.05")).json()

    assert body["recommendation"]["decision"] == EXIT
    assert body["recommendation"]["confidence"] == "medium"


def test_mild_borrower_shock_stays_when_expected_loss_is_below_exit_cost(api, morpho_client, clock):
    body = post(api(build_service(morpho_client, clock)), borrower_request("0.05")).json()

    assert_contract(body)
    assert body["recommendation"]["decision"] == STAY
    assert body["scenario_results"][0]["hard_triggers"] == []


def test_unavailable_probability_with_impact_is_unavailable(api, morpho_client, clock):
    service = build_service(morpho_client, clock, probabilities=FakeProbabilities({}))

    body = post(api(service), borrower_request("0.05")).json()

    assert_contract(body)
    assert body["recommendation"]["decision"] == UNAVAILABLE


# Orchestration quality: quote freshness and refresh


def analyze(service, payload):
    return service.analyze(AnalysisRequest.model_validate(payload))


@pytest.mark.parametrize(
    ("ages", "blocks"),
    [((31,), (SNAPSHOT_BLOCK,)), ((0,), (SNAPSHOT_BLOCK + 21,))],
    ids=["quote-age-31s", "block-lag-21"],
)
def test_stale_quote_without_successful_refresh_is_unavailable(morpho_client, clock, ages, blocks):
    quotes = FakeQuotes(clock, ages=ages, blocks=blocks)
    service = build_service(morpho_client, clock, quotes=quotes)

    response = analyze(service, borrower_request("0.05"))

    assert quotes.calls == [False, True]  # refreshed exactly once
    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE
    assert response.exit_estimate.freshness is FreshnessStatus.EXPIRED


@pytest.mark.parametrize(
    ("ages", "blocks"),
    [((31, 0), (SNAPSHOT_BLOCK,)), ((0,), (SNAPSHOT_BLOCK + 21, SNAPSHOT_BLOCK + 2))],
    ids=["quote-age-31s", "block-lag-21"],
)
def test_stale_quote_with_successful_refresh_supports_a_decision(morpho_client, clock, ages, blocks):
    quotes = FakeQuotes(clock, ages=ages, blocks=blocks)
    service = build_service(morpho_client, clock, quotes=quotes)

    response = analyze(service, borrower_request("0.05"))

    assert quotes.calls == [False, True]
    assert response.recommendation.decision is RecommendationDecision.STAY
    assert response.exit_estimate.freshness is FreshnessStatus.FRESH


def test_quote_at_twenty_block_lag_needs_no_refresh(morpho_client, clock):
    quotes = FakeQuotes(clock, blocks=(SNAPSHOT_BLOCK + 20,))

    response = analyze(build_service(morpho_client, clock, quotes=quotes), borrower_request("0.05"))

    assert quotes.calls == [False]
    assert response.recommendation.decision is RecommendationDecision.STAY


def test_expected_loss_is_central_probability_times_impact_without_double_counting(
    morpho_client, clock
):
    # Each severity has its own probability (the milder shock is the likelier one),
    # so pairing a probability with the wrong severity's impact changes the result.
    probabilities = FakeProbabilities(
        {
            "collateral-shock@0.05": "0.001",
            "collateral-shock@0.04": "0.01",
            "protocol-impairment": "0.0004",
        }
    )
    service = build_service(morpho_client, clock, probabilities=probabilities)
    payload = borrower_request(
        scenarios=[
            {"scenario_id": "collateral-shock", "severity": "0.05"},
            {"scenario_id": "collateral-shock", "severity": "0.04"},
            {"scenario_id": "protocol-impairment", "severity": "0.10"},
        ]
    )

    response = analyze(service, payload)

    five, four, protocol = response.scenario_results
    assert five.probability.central == Decimal("0.001")
    assert four.probability.central == Decimal("0.01")
    assert five.impact > four.impact > 0
    shock = max(five.impact * Decimal("0.001"), four.impact * Decimal("0.01"))
    expected = shock + protocol.impact * Decimal("0.0004")
    assert response.recommendation.expected_loss == expected.quantize(Decimal("1E-18"))


SEVERITY_PROBABILITIES = FakeProbabilities(
    {
        "collateral-shock@0.05": "0.10",
        "collateral-shock@0.50": "0.001",
        "protocol-impairment": "0.0004",
    }
)
TWO_SEVERITIES = [
    {"scenario_id": "collateral-shock", "severity": "0.05"},
    {"scenario_id": "collateral-shock", "severity": "0.50"},
]


@pytest.mark.parametrize("mode", ["lender", "borrower"])
def test_each_severity_is_paired_with_its_own_probability(morpho_client, clock, mode):
    if mode == "lender":
        morpho = RecordedAllocationsOnly(morpho_client)
        payload = lender_vault_request(scenarios=TWO_SEVERITIES)
    else:
        morpho = morpho_client
        payload = borrower_request(scenarios=TWO_SEVERITIES)
    service = build_service(morpho, clock, probabilities=SEVERITY_PROBABILITIES)

    response = analyze(service, payload)

    mild, severe = response.scenario_results
    assert mild.probability.central == Decimal("0.10")
    assert severe.probability.central == Decimal("0.001")
    expected = max(mild.impact * Decimal("0.10"), severe.impact * Decimal("0.001"))
    assert response.recommendation.expected_loss == expected.quantize(Decimal("1E-18"))
    # The mild probability is never applied to the severe impact.
    assert response.recommendation.expected_loss < severe.impact * Decimal("0.10")


# Conflicting probability sources


def test_combine_conservative_selects_the_higher_estimate_and_lowers_confidence():
    first = estimate("0.02", source="chainlink:ETH/USD")
    second = estimate("0.05", source="fixture:ETH/USD-secondary")

    combined = combine_conservative("probability:collateral-shock", (first, second))

    assert combined.estimate.central == Decimal("0.05")
    assert combined.estimate.high == Decimal("0.1")
    assert combined.estimate.confidence is ConfidenceLevel.MEDIUM
    (conflict,) = combined.conflicts
    assert conflict.values == ("0.02", "0.05")
    assert conflict.sources == ("chainlink:ETH/USD", "fixture:ETH/USD-secondary")


def test_combine_conservative_with_agreeing_sources_keeps_confidence():
    combined = combine_conservative(
        "probability:collateral-shock", (estimate("0.02"), estimate("0.02", source="other"))
    )

    assert combined.conflicts == ()
    assert combined.estimate.confidence is ConfidenceLevel.HIGH


class FakeHistory:
    def __init__(self, centrals: dict[str, str | None]):
        self.centrals = centrals
        self.calls: list[str] = []

    def estimate(self, feed, start, end, event, *, now):
        self.calls.append(feed)
        central = self.centrals[feed]
        if central is None:
            probability = ProbabilityEstimate(
                method=ProbabilityMethod.UNAVAILABLE,
                low=None,
                central=None,
                high=None,
                confidence=ConfidenceLevel.UNAVAILABLE,
                horizon_days=30,
                source=f"chainlink:{feed}",
                retrieved_at=now,
                freshness=FreshnessStatus.EXPIRED,
                explanation="unavailable",
            )
        else:
            probability = estimate(central, source=f"chainlink:{feed}", now=now)
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
            threshold_definition=getattr(event, "description", "event"),
        )


def test_conflicting_sources_select_conservative_value_and_lower_recommendation_confidence(
    morpho_client, clock
):
    history = FakeHistory({"ETH/USD": "0.001", "ETH/USD-secondary": "0.003"})
    provider = HybridProbabilityProvider(
        history,
        evidence=None,
        collateral_feeds={"WETH": ("ETH/USD", "ETH/USD-secondary")},
    )
    service = build_service(morpho_client, clock, probabilities=provider)
    agreeing = build_service(
        morpho_client,
        clock,
        probabilities=HybridProbabilityProvider(
            FakeHistory({"ETH/USD": "0.001"}), evidence=None, collateral_feeds={"WETH": ("ETH/USD",)}
        ),
    )

    response = analyze(service, borrower_request("0.05"))
    baseline = analyze(agreeing, borrower_request("0.05"))

    (result,) = response.scenario_results
    assert result.probability.central == Decimal("0.003")
    assert result.probability.confidence is ConfidenceLevel.MEDIUM
    assert response.recommendation.decision is RecommendationDecision.STAY
    order = ["unavailable", "low", "medium", "high"]
    assert order.index(response.recommendation.confidence.value) < order.index(
        baseline.recommendation.confidence.value
    )
    assert "conflict" in response.recommendation.explanation.lower()


def test_empirical_provider_uses_the_feed_for_the_shocked_collateral(morpho_client, clock):
    history = FakeHistory({"ETH/USD": "0.002"})
    provider = HybridProbabilityProvider(history, evidence=None)
    service = build_service(morpho_client, clock, probabilities=provider)

    response = analyze(service, borrower_request("0.05"))

    assert history.calls == ["ETH/USD"]
    (result,) = response.scenario_results
    assert result.probability.method is ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW
    assert result.probability.central == Decimal("0.002")


def test_empirical_provider_without_a_feed_is_explicitly_unavailable(morpho_client, clock):
    history = FakeHistory({})
    provider = HybridProbabilityProvider(history, evidence=None)
    payload = borrower_request("0.05")
    payload["position"].update(
        entity_id="wbtc-usdc-860", collateral_amount="1", debt_amount="60000", amount="60000", ltv="0.73"
    )

    response = analyze(build_service(morpho_client, clock, probabilities=provider), payload)

    (result,) = response.scenario_results
    assert history.calls == []
    assert result.probability.method is ProbabilityMethod.UNAVAILABLE
    assert result.probability.central is None
    assert "WBTC" in result.probability.explanation
    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE


def test_protocol_probability_is_model_estimated_and_labelled(morpho_client, clock):
    from app.evidence.models import (
        CombinedEvidence,
        EvidenceBundle,
        EvidenceDimension,
        EvidenceFactor,
        EvidenceSource,
        EvidenceStatus,
    )

    class FakeEvidence:
        def evidence(self, protocol, *, now):
            factors = tuple(
                EvidenceFactor(
                    dimension=dimension,
                    value=Decimal("90"),
                    source=EvidenceSource.DEFISCAN,
                    source_url="https://www.defiscan.info/data/morpho/compiled-review.json",
                    observed_at=now - timedelta(days=10),
                    rule="fixture",
                )
                for dimension in EvidenceDimension
            )
            bundle = EvidenceBundle(
                protocol=protocol,
                source=EvidenceSource.DEFISCAN,
                status=EvidenceStatus.AVAILABLE,
                source_url="https://www.defiscan.info/data/morpho/compiled-review.json",
                methodology_url="https://www.defiscan.info/about",
                retrieved_at=now,
                source_updated_at=now - timedelta(days=10),
                factors=factors,
                observations=(),
            )
            return CombinedEvidence(protocol=protocol, bundles=(bundle,), evaluated_at=now)

    provider = HybridProbabilityProvider(None, evidence=FakeEvidence())
    selection = ScenarioSelection(scenario_id=ScenarioId.PROTOCOL_IMPAIRMENT, severity=Decimal("0.1"))

    assessment = provider.assess(selection, None, None, now=clock())

    assert assessment.estimate.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert "model-estimated" in assessment.estimate.explanation


def test_default_wiring_without_rpc_url_degrades_to_unavailable_not_errors(monkeypatch):
    from app.api.analysis import build_default_analysis_service

    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)

    service = build_default_analysis_service()

    assert isinstance(service, AnalysisService)


def test_price_shock_event_matches_a_brute_force_24_hour_drop_check():
    from app.analysis.service import PriceShockEvent
    from app.history.estimator import RollingWindowEstimator
    from app.history.normalization import NormalizedSeries
    from app.infrastructure.freshness import Freshness

    start = datetime(2026, 1, 1, tzinfo=UTC)
    values = []
    for hour in range(24 * 40):
        value = Decimal(2000) + Decimal(hour % 37)
        if hour in (400, 401):
            value = Decimal(1690)  # -16% within 24 h of hour 399
        if hour == 600:
            value = Decimal(1800)  # -10%: below the 15% threshold
        if 800 <= hour < 830:
            value = Decimal(2000) - Decimal(12) * (hour - 800)  # slow 18% slide over 30 h
        values.append(value)
    series = NormalizedSeries(
        observations=tuple(
            Observation(timestamp=start + timedelta(hours=i), value=v, block_number=i, log_index=0)
            for i, v in enumerate(values)
        ),
        gaps=(),
        start=start,
        end=start + timedelta(hours=len(values) - 1),
        source="fixture",
        retrieved_at=start,
        freshness=Freshness.FRESH,
    )

    def brute(window):
        return any(
            window[j].value <= window[i].value * Decimal("0.85")
            for j in range(len(window))
            for i in range(max(0, j - 24), j)
        )

    fast = RollingWindowEstimator().estimate_with_evidence(series, PriceShockEvent(Decimal("0.15")))
    slow = RollingWindowEstimator().estimate_with_evidence(series, brute)

    assert fast.event_window_count == slow.event_window_count > 0
    assert fast.sample_dates == slow.sample_dates


def test_depeg_event_fires_at_or_below_the_threshold():
    from app.analysis.service import DepegEvent

    def window(*values):
        return tuple(
            Observation(timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i), value=Decimal(v), block_number=i, log_index=0)
            for i, v in enumerate(values)
        )

    event = DepegEvent(Decimal("0.10"))
    assert event(window("1", "0.95", "0.90"))
    assert not event(window("1", "0.95", "0.9001"))


# Live oracle data is critical (spec § 13 hard trigger, § 15 missing -> UNAVAILABLE)


class RaisingOracles:
    def observe(self, market, *, now):
        raise RuntimeError("oracle provider said https://rpc.example.invalid/v2/fake-rpc-credential")


class NoObservation:
    def observe(self, market, *, now):
        return None


@pytest.mark.parametrize(
    "oracles", [None, RaisingOracles(), NoObservation()], ids=["no-provider", "error", "none"]
)
@pytest.mark.parametrize("mode", ["lender", "borrower"])
def test_missing_live_oracle_data_is_unavailable(api, morpho_client, clock, caplog, oracles, mode):
    if mode == "lender":
        service = build_service(RecordedAllocationsOnly(morpho_client), clock, oracles=oracles)
        payload = lender_vault_request()
    else:
        service = build_service(morpho_client, clock, oracles=oracles)
        payload = borrower_request("0.05")

    with caplog.at_level(logging.DEBUG):
        response = post(api(service), payload)

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    assert body["recommendation"]["decision"] == UNAVAILABLE
    assert "live oracle" in body["recommendation"]["explanation"]
    for leaked in ("rpc.example.invalid", "fake-rpc-credential"):
        assert leaked not in response.text
        assert leaked not in service_logs(caplog)


class FakeChainlink:
    """`EthereumClient.get_chainlink_observations` over configured feed histories."""

    def __init__(self, histories=None, *, fail=False):
        self.histories = histories or {}
        self.fail = fail
        self.calls: list[str] = []

    def get_chainlink_observations(self, feed, start, end):
        self.calls.append(feed)
        if self.fail:
            from app.adapters.ethereum import EthereumProviderError

            raise EthereumProviderError("request to https://rpc.example.invalid/v2/fake-rpc-credential failed")
        if feed not in self.histories:
            from app.adapters.ethereum import UnsupportedFeedError

            raise UnsupportedFeedError(f"unsupported Chainlink feed: {feed}")
        return tuple(item for item in self.histories[feed] if start <= item.timestamp <= end)


def weth_usdc_market(morpho_client):
    entry = next(
        item for item in CatalogRepository(CATALOG_PATH).list_entries() if item.entity_id == "weth-usdc-860"
    )
    return morpho_client.get_market(entry.address_or_market_id).value


def feed_point(clock, value, *, age_seconds):
    return Observation(
        timestamp=clock() - timedelta(seconds=age_seconds),
        value=Decimal(value),
        block_number=SNAPSHOT_BLOCK,
        log_index=0,
        source="chainlink:fixture",
    )


def chainlink_service(morpho, clock, client, **kwargs):
    from app.analysis.service import ChainlinkLatest, ChainlinkOracleHealth, ChainlinkPrices

    reader = ChainlinkLatest(client, clock=clock)
    return build_service(
        morpho,
        clock,
        oracles=ChainlinkOracleHealth(reader),
        prices=ChainlinkPrices(reader),
        **kwargs,
    )


def healthy_histories(market, clock, *, eth_age=600, eth_value=None):
    return {
        "ETH/USD": (
            feed_point(clock, eth_value or market.collateral_asset.price_usd, age_seconds=eth_age),
        ),
        "USDC/USD": (feed_point(clock, market.loan_asset.price_usd, age_seconds=3 * 3600),),
    }


def test_chainlink_oracle_health_with_healthy_feeds_does_not_trigger(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = FakeChainlink(healthy_histories(market, clock))

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is RecommendationDecision.STAY
    assert response.recommendation.triggers == ()
    oracle = next(node for node in response.graph.nodes if node.node_type.value == "oracle")
    assert oracle.metrics["age_seconds"] == Decimal(0)  # within the heartbeat
    assert oracle.metrics["deviation"] == Decimal(0)


def test_chainlink_oracle_health_reports_staleness_beyond_the_heartbeat(morpho_client, clock):
    from app.analysis.service import ChainlinkLatest, ChainlinkOracleHealth

    market = weth_usdc_market(morpho_client)
    client = FakeChainlink(healthy_histories(market, clock, eth_age=3600 + 1799))

    envelope = ChainlinkOracleHealth(ChainlinkLatest(client, clock=clock)).observe(market, now=clock())

    assert envelope is not None
    # ETH/USD heartbeat is 3600 s: 1799 s beyond it; USDC/USD is inside its 86400 s heartbeat.
    assert envelope.value.age_seconds == Decimal(1799)
    assert envelope.value.oracle_address == market.oracle_address
    assert "beyond" in envelope.source


@pytest.mark.parametrize(
    ("eth_age", "decision"),
    [(3600 + 1799, RecommendationDecision.STAY), (3600 + 1800, RecommendationDecision.EXIT)],
    ids=["heartbeat+29m59s", "heartbeat+30m"],
)
def test_chainlink_feed_stale_for_thirty_minutes_beyond_its_heartbeat_exits(
    morpho_client, clock, eth_age, decision
):
    market = weth_usdc_market(morpho_client)
    client = FakeChainlink(healthy_histories(market, clock, eth_age=eth_age))

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is decision
    if decision is RecommendationDecision.EXIT:
        oracle = node_of(response.graph, "oracle")
        assert response.recommendation.triggers == (f"live:oracle_failure:{oracle}",)
        assert response.recommendation.dominant_dependency == oracle


@pytest.mark.parametrize(
    ("factor", "decision"),
    [("1.049", RecommendationDecision.STAY), ("1.05", RecommendationDecision.EXIT)],
    ids=["4.9%", "5%"],
)
def test_chainlink_feed_deviating_five_percent_from_the_reference_exits(
    morpho_client, clock, factor, decision
):
    market = weth_usdc_market(morpho_client)
    eth_value = market.collateral_asset.price_usd * Decimal(factor)
    client = FakeChainlink(healthy_histories(market, clock, eth_value=eth_value))

    response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is decision


def test_chainlink_provider_error_is_unavailable_and_sanitized(morpho_client, clock, caplog):
    client = FakeChainlink(fail=True)

    with caplog.at_level(logging.DEBUG):
        response = analyze(chainlink_service(morpho_client, clock, client), borrower_request("0.05"))

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE
    assert "live oracle" in response.recommendation.explanation
    assert "rpc.example.invalid" not in service_logs(caplog)
    assert "fake-rpc-credential" not in response.model_dump_json()


def test_chainlink_feed_without_updates_in_the_lookback_is_unavailable(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    histories = healthy_histories(market, clock)
    histories["ETH/USD"] = ()

    response = analyze(
        chainlink_service(morpho_client, clock, FakeChainlink(histories)), borrower_request("0.05")
    )

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE


def test_oracle_without_a_supported_chainlink_feed_is_unavailable(morpho_client, clock):
    # The recorded MEV vault allocation is the wstETH/WETH market: wstETH has no feed.
    market = weth_usdc_market(morpho_client)
    client = FakeChainlink(healthy_histories(market, clock))

    response = analyze(
        chainlink_service(RecordedAllocationsOnly(morpho_client), clock, client),
        lender_vault_request(),
    )

    assert response.recommendation.decision is RecommendationDecision.UNAVAILABLE
    assert "live oracle" in response.recommendation.explanation
    assert "wstETH" not in client.calls


def test_chainlink_lookups_are_memoized_across_prices_and_oracle_health(morpho_client, clock):
    market = weth_usdc_market(morpho_client)
    client = FakeChainlink(healthy_histories(market, clock))
    service = chainlink_service(morpho_client, clock, client)

    analyze(service, borrower_request("0.05"))
    analyze(service, borrower_request("0.05"))

    assert sorted(client.calls) == ["ETH/USD", "USDC/USD"]


def node_of(graph, node_type):
    return next(node.node_id for node in graph.nodes if node.node_type.value == node_type)


# Wiring and construction failures


def test_default_wiring_with_an_rpc_url_uses_chainlink_oracle_health(monkeypatch, tmp_path):
    from app.analysis.service import ChainlinkOracleHealth
    from app.api.analysis import build_default_analysis_service

    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://rpc.example.invalid/v2/fake-rpc-credential")
    monkeypatch.setenv("RISK0_CACHE_PATH", str(tmp_path / "cache.sqlite3"))

    service = build_default_analysis_service()

    assert isinstance(service._oracles, ChainlinkOracleHealth)
    assert service._oracles.reader is service._prices.reader


def test_default_wiring_without_rpc_url_has_no_oracle_provider(monkeypatch, tmp_path):
    from app.api.analysis import build_default_analysis_service

    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)
    monkeypatch.setenv("RISK0_CACHE_PATH", str(tmp_path / "cache.sqlite3"))

    service = build_default_analysis_service()

    # No oracle provider: every graph oracle is missing critical data -> UNAVAILABLE.
    assert service._oracles is None


def test_service_construction_failure_returns_a_sanitized_503(monkeypatch, caplog):
    import app.api.analysis as module

    def broken():
        raise PermissionError("cannot open /secret/cache/risk0.sqlite3 via https://rpc.example.invalid")

    monkeypatch.setattr(module, "build_default_analysis_service", broken)
    module.get_default_service.cache_clear()
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.DEBUG):
        response = post(client, borrower_request())
    module.get_default_service.cache_clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "The risk analysis is temporarily unavailable."}
    for leaked in ("/secret", "rpc.example.invalid", "sqlite3", "Traceback"):
        assert leaked not in response.text
        assert leaked not in service_logs(caplog)


# Protocol evidence cache


def test_defiscan_review_is_cached_for_a_day(clock):
    from app.analysis.service import FileAndDefiScanEvidence
    from app.evidence.models import EvidenceBundle, EvidenceSource

    class CountingDefiScan:
        calls = 0

        def get_review(self, slug):
            CountingDefiScan.calls += 1
            return EvidenceBundle.unavailable(
                protocol=slug,
                source=EvidenceSource.DEFISCAN,
                source_url="https://www.defiscan.info/data/morpho/compiled-review.json",
                methodology_url="https://www.defiscan.info/about",
                retrieved_at=clock(),
                reason="fixture",
            )

    path = ROOT / "data" / "evidence" / "defisafety.ethereum.json"
    evidence = FileAndDefiScanEvidence(path, CountingDefiScan(), clock=clock)

    evidence.evidence("morpho", now=clock())
    evidence.evidence("morpho", now=clock())
    assert CountingDefiScan.calls == 1

    clock.advance(timedelta(hours=24, seconds=1))
    evidence.evidence("morpho", now=clock())
    assert CountingDefiScan.calls == 2


# Lender withdrawal trigger at the 20% threshold (spec § 13), through the service


@pytest.mark.parametrize(("ratio", "triggered"), [("0.19", True), ("0.21", False)])
def test_lender_stressed_withdrawable_liquidity_threshold(morpho_client, clock, ratio, triggered):
    morpho = RecordedAllocationsOnly(morpho_client)
    service = build_service(morpho, clock)
    severity = Decimal("0.50")
    scenarios = [{"scenario_id": "liquidity-collapse", "severity": str(severity)}]
    probe = analyze(service, lender_vault_request(amount="1", scenarios=scenarios))
    vault = next(node for node in probe.graph.nodes if node.node_type.value == "vault")
    stressed = vault.metrics["available_liquidity"] * (1 - severity)
    amount = (stressed / Decimal(ratio)).quantize(Decimal("1E-6"))

    response = analyze(service, lender_vault_request(amount=str(amount), scenarios=scenarios))

    (result,) = response.scenario_results
    assert result.stressed_values["stressed_available_liquidity"] == stressed
    if triggered:
        assert result.hard_triggers == ("withdrawal_shortfall",)
        assert response.recommendation.decision is RecommendationDecision.EXIT
        assert response.recommendation.triggers == ("liquidity-collapse:withdrawal_shortfall",)
    else:
        assert result.hard_triggers == ()
        assert response.recommendation.triggers == ()
