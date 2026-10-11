"""Protocol impairment from the DeFiLlama base rate, and Morpho TVL concentration, through the service."""

import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from app.analysis.service import (
    PROTOCOL_TVL_UNAVAILABLE,
    AnalysisService,
    DefiLlamaProtocolTvl,
    FileAndDefiScanEvidence,
    HybridProbabilityProvider,
    combine_protocol_estimates,
    unavailable_estimate,
)
from app.catalog.repository import CatalogRepository
from app.contracts.models import (
    ConfidenceLevel,
    ProbabilityMethod,
    ScenarioId,
    ScenarioSelection,
)
from app.evidence.models import EvidenceBundle, EvidenceSource
from tests.api.test_analysis import (
    CATALOG_PATH,
    ROOT,
    FakeGas,
    FakeIncidents,
    FakeOracles,
    FakePrices,
    FakeProbabilities,
    FakeQuotes,
    RecordedAllocationsOnly,
    api,  # noqa: F401 - pytest fixture
    assert_contract,
    estimate,
    lender_vault_request,
    post,
    service_logs,
)
from tests.fixtures.defillama.recorded import FakeDefiLlamaApi


DEFISAFETY_PATH = ROOT / "data" / "evidence" / "defisafety.ethereum.json"
PROTOCOL = ScenarioSelection(scenario_id=ScenarioId.PROTOCOL_IMPAIRMENT, severity=Decimal("0.3"))


class UnavailableDefiScan:
    def __init__(self, clock) -> None:
        self.clock = clock

    def get_review(self, slug):
        return EvidenceBundle.unavailable(
            protocol=slug,
            source=EvidenceSource.DEFISCAN,
            source_url=f"https://www.defiscan.info/data/{slug}/compiled-review.json",
            methodology_url="https://www.defiscan.info/about",
            retrieved_at=self.clock(),
            reason=f"DeFiScan index has no review for {slug!r}",
        )


def evidence_source(clock, defillama_api=None):
    defillama = (defillama_api or FakeDefiLlamaApi()).client(clock)
    return FileAndDefiScanEvidence(
        DEFISAFETY_PATH, UnavailableDefiScan(clock), defillama=defillama, clock=clock
    )


def test_without_evidence_scores_the_base_rate_is_used_alone_at_low_confidence(clock):
    provider = HybridProbabilityProvider(None, evidence_source(clock))

    result = provider.assess(PROTOCOL, None, None, now=clock()).estimate

    assert result.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert result.confidence is ConfidenceLevel.LOW
    assert result.central is not None and result.low < result.central < result.high
    assert result.source.startswith("defillama https://api.llama.fi/hacks")
    assert result.explanation.startswith("Empirical industry base rate")
    assert "Used alone because protocol evidence scores are unavailable" in result.explanation
    assert "DeFiScan index has no review for 'morpho'" in result.explanation
    assert "LeadBlock's Morpho Blue Market" in result.explanation


def test_the_base_rate_is_computed_once_per_data_version(clock, monkeypatch):
    import app.evidence.defillama as module

    calls = []
    original = module.base_rate_estimate

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "base_rate_estimate", counting)
    source = evidence_source(clock)

    first = source.impairment_base_rate("morpho", now=clock())
    second = source.impairment_base_rate("morpho", now=clock())

    assert first == second
    assert len(calls) == 1


def test_defillama_outage_is_unavailable_and_sanitized(clock, caplog):
    outage = FakeDefiLlamaApi()
    outage.mode = "down"
    source = evidence_source(clock, outage)

    with caplog.at_level(logging.WARNING, logger="risk0"):
        result = HybridProbabilityProvider(None, source).assess(PROTOCOL, None, None, now=clock())

    assert result.estimate.method is ProbabilityMethod.UNAVAILABLE
    assert result.estimate.central is None
    assert "DeFiLlama exploit and protocol data could not be retrieved" in result.estimate.explanation
    assert "DeFiScan index has no review" in result.estimate.explanation
    for leaked in ("secret.example", "simulated outage"):
        assert leaked not in result.estimate.explanation
        assert leaked not in service_logs(caplog)
    assert "defillama unavailable: DefiLlamaUnavailableError" in service_logs(caplog)


def test_no_defillama_source_is_explicitly_unavailable(clock):
    source = FileAndDefiScanEvidence(DEFISAFETY_PATH, UnavailableDefiScan(clock), clock=clock)

    result = source.impairment_base_rate("morpho", now=clock())

    assert result.method is ProbabilityMethod.UNAVAILABLE
    assert result.explanation == "No DeFiLlama source is configured."


def scored(central: str, clock):
    return estimate(central, source="defiscan review", confidence=ConfidenceLevel.HIGH, now=clock()).model_copy(
        update={"method": ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL}
    )


def test_with_both_estimates_the_higher_is_used_and_the_conflict_kept(clock):
    base = scored("0.002", clock).model_copy(
        update={"source": "defillama", "confidence": ConfidenceLevel.LOW}
    )

    result = combine_protocol_estimates(scored("0.0008", clock), base)

    assert result.estimate.central == Decimal("0.002")
    assert result.estimate.confidence is ConfidenceLevel.LOW
    (conflict,) = result.conflicts
    assert conflict.field == "probability:protocol-impairment"
    assert conflict.sources == ("defiscan review", "defillama")


def test_without_a_base_rate_the_evidence_score_is_unchanged(clock):
    evidence = scored("0.0008", clock)

    result = combine_protocol_estimates(evidence, unavailable_estimate(clock(), "no data"))

    assert result.estimate == evidence


def test_with_neither_estimate_both_reasons_are_reported(clock):
    result = combine_protocol_estimates(
        unavailable_estimate(clock(), "No scores."), unavailable_estimate(clock(), "No base rate.")
    )

    assert result.estimate.method is ProbabilityMethod.UNAVAILABLE
    assert result.estimate.explanation == "No scores. Empirical base rate: No base rate."


# TVL concentration through the service


def service(morpho_client, clock, protocol_tvl):
    return AnalysisService(
        CatalogRepository(CATALOG_PATH),
        RecordedAllocationsOnly(morpho_client),
        probabilities=FakeProbabilities(
            {"collateral-shock": "0.001", "protocol-impairment": "0.0004"}
        ),
        quotes=FakeQuotes(clock),
        gas=FakeGas(),
        prices=FakePrices(),
        oracles=FakeOracles(clock),
        incidents=FakeIncidents(),
        clock=clock,
        protocol_tvl=protocol_tvl,
    )


def morpho_node(body):
    return next(node for node in body["graph"]["nodes"] if node["node_id"] == "protocol:1:morpho")


def test_protocol_node_carries_defillama_tvl_concentration(api, morpho_client, clock):  # noqa: F811
    provider = DefiLlamaProtocolTvl(FakeDefiLlamaApi().client(clock))
    client = api(service(morpho_client, clock, provider))

    response = post(client, lender_vault_request())

    assert response.status_code == 200
    body = response.json()
    assert_contract(body)
    metrics = morpho_node(body)["metrics"]
    assert metrics["chain_tvl_share"] == "0.100259"
    assert metrics["chain_tvl_usd"] == "5165760890.8268795"
    assert Decimal(metrics["entity_share_of_protocol_tvl"]) > 0
    assert morpho_node(body)["provenance"].startswith("risk0 curated catalog; TVL: defillama")
    warnings = body["graph"]["completeness_warnings"]
    assert any(item.startswith("TVL concentration: Morpho Blue holds 10.0%") for item in warnings)
    assert PROTOCOL_TVL_UNAVAILABLE not in warnings


def test_tvl_provider_failure_is_a_completeness_warning(api, morpho_client, clock, caplog):  # noqa: F811
    outage = FakeDefiLlamaApi()
    outage.mode = "error"
    client = api(service(morpho_client, clock, DefiLlamaProtocolTvl(outage.client(clock))))

    with caplog.at_level(logging.WARNING, logger="risk0"):
        response = post(client, lender_vault_request())

    assert response.status_code == 200
    body = response.json()
    assert PROTOCOL_TVL_UNAVAILABLE in body["graph"]["completeness_warnings"]
    assert "chain_tvl_share" not in morpho_node(body)["metrics"]
    assert "secret-token-123" not in response.text
    assert "secret-token-123" not in service_logs(caplog)


def test_without_a_tvl_provider_there_is_no_tvl_warning(api, morpho_client, clock):  # noqa: F811
    client = api(service(morpho_client, clock, None))

    body = post(client, lender_vault_request()).json()

    assert PROTOCOL_TVL_UNAVAILABLE not in body["graph"]["completeness_warnings"]


def test_stale_defillama_data_marks_the_protocol_node_stale(morpho_client, clock, tmp_path):
    from app.infrastructure.cache import SQLiteCache

    cache = SQLiteCache(tmp_path / "tvl.sqlite3")
    recorded = FakeDefiLlamaApi()
    DefiLlamaProtocolTvl(recorded.client(clock, cache)).protocol_tvl("morpho", now=clock())
    recorded.mode = "down"
    clock.advance(timedelta(days=2))

    envelope = DefiLlamaProtocolTvl(recorded.client(clock, cache)).protocol_tvl("morpho", now=clock())

    assert envelope.freshness.value == "stale_usable"
    assert envelope.value.chain_tvl_share == Decimal("0.100259")


@pytest.mark.parametrize("protocol", ["unknown"])
def test_unknown_protocol_has_no_tvl(clock, protocol):
    assert DefiLlamaProtocolTvl(FakeDefiLlamaApi().client(clock)).protocol_tvl(protocol, now=clock()) is None
