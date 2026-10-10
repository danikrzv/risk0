import io
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters import defiscan
from app.adapters.defiscan import (
    DEFISCAN_BASE_URL,
    DefiScanClient,
    DefiScanUnavailableError,
    urllib_fetch,
)
from app.evidence.models import (
    EvidenceDimension,
    EvidenceFormatError,
    EvidenceNotFoundError,
    EvidenceSource,
    EvidenceStatus,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
INDEX_URL = f"{DEFISCAN_BASE_URL}/data/index.json"
REVIEW_URL = f"{DEFISCAN_BASE_URL}/data/sample-protocol/compiled-review.json"
RETRIEVED_AT = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        if url not in self._responses:
            raise EvidenceNotFoundError(url)
        return self._responses[url]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_client(responses: dict[str, bytes] | None = None) -> tuple[DefiScanClient, FakeFetcher]:
    if responses is None:
        responses = {
            INDEX_URL: fixture_bytes("defiscan-index.json"),
            REVIEW_URL: fixture_bytes("defiscan-review.json"),
        }
    fetcher = FakeFetcher(responses)
    return DefiScanClient(fetch=fetcher, clock=lambda: RETRIEVED_AT), fetcher


def observations(bundle, category: str) -> list:
    return [item for item in bundle.observations if item.category == category]


def test_index_lists_covered_projects_with_decimal_coverage():
    client, fetcher = make_client()

    projects = client.get_index()

    assert fetcher.requested == [INDEX_URL]
    assert [project.slug for project in projects] == [
        "sample-protocol",
        "sample-base-vault",
    ]
    sample = projects[0]
    assert sample.name == "Sample Protocol"
    assert sample.chain == "Ethereum"
    assert sample.project_type == "lending"
    assert sample.verified is True
    assert sample.coverage == Decimal("100")
    assert isinstance(sample.coverage, Decimal)


def test_review_bundle_is_attributed_and_timestamped():
    client, fetcher = make_client()

    bundle = client.get_review("sample-protocol")

    assert fetcher.requested == [INDEX_URL, REVIEW_URL]
    assert bundle.status is EvidenceStatus.AVAILABLE
    assert bundle.protocol == "sample-protocol"
    assert bundle.source is EvidenceSource.DEFISCAN
    assert bundle.source_url == REVIEW_URL
    assert bundle.methodology_url == "https://www.defiscan.info/about"
    assert bundle.retrieved_at == RETRIEVED_AT
    assert bundle.source_updated_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    timestamps = {item.label: item.observed_at for item in observations(bundle, "timestamp")}
    assert timestamps == {
        "publishedAt": datetime(2026, 4, 1, tzinfo=UTC),
        "lastModified": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        "compiledAt": datetime(2026, 9, 30, 13, 36, 34, tzinfo=UTC),
    }


def test_review_preserves_admin_upgradeability_and_governance_fields():
    client, _ = make_client()

    bundle = client.get_review("sample-protocol")

    admins = {item.label: item for item in observations(bundle, "admin")}
    assert admins["Ops EOA"].value == "EOA"
    assert admins["Ops EOA"].reference == "eth:0x0000000000000000000000000000000000000a02"
    assert admins["Sample Timelock (48h)"].value == "Immutable"
    assert admins["Guardian 3/5 Multisig"].value == "Multisig"

    upgradeability = {item.label: item.value for item in observations(bundle, "upgradeability")}
    assert upgradeability == {
        "SampleVault": "EIP1967 proxy",
        "SampleRouter": "immutable",
        "SampleClone": "EIP1167 proxy",
        "External Feed": "EIP1967 proxy (external)",
    }

    governance = {item.label: item.value for item in observations(bundle, "governance")}
    assert governance == {
        "framework": "Governor Bravo",
        "voteExecution": "onchain",
        "executionDelay.seconds": "172800",
    }


def test_review_preserves_dependency_audit_and_update_records():
    client, _ = make_client()

    bundle = client.get_review("sample-protocol")

    dependencies = [(item.label, item.value) for item in observations(bundle, "dependency")]
    assert dependencies == [
        ("Chainlink ETH/USD", "Chainlink"),
        ("Unlabelled Adapter", "unknown entity"),
    ]

    audits = observations(bundle, "audit")
    assert [(item.label, item.value, item.reference) for item in audits] == [
        ("Example Audits LLC", "2025-11 Core contracts", "https://example.com/audits/core.pdf"),
        ("Second Review GmbH", "2026-02-14 Vault upgrade", "https://example.com/audits/vault.pdf"),
    ]

    updates = observations(bundle, "update")
    assert [(item.label, item.value, item.observed_at) for item in updates] == [
        ("SampleVault", "data-change", datetime(2026, 9, 15, 8, 0, tzinfo=UTC)),
        ("SampleVault", "implementation-change", datetime(2026, 8, 1, tzinfo=UTC)),
    ]


def test_review_normalizes_only_dimensions_defiscan_covers():
    client, _ = make_client()

    bundle = client.get_review("sample-protocol")

    factors = {factor.dimension: factor for factor in bundle.factors}
    assert set(factors) == {
        EvidenceDimension.AUDITS_TESTING,
        EvidenceDimension.CONTROLS_GOVERNANCE,
        EvidenceDimension.DEPENDENCIES,
    }
    # 100 - 15 * 2 unmitigated critical functions - 20 EOA critical admin
    # - 20 upgradeable internal contract.
    assert factors[EvidenceDimension.CONTROLS_GOVERNANCE].value == Decimal("30")
    # 100 - 15 * one named dependency entity - 30 for an unknown entity.
    assert factors[EvidenceDimension.DEPENDENCIES].value == Decimal("55")
    # min(60, 15 * 2 audits) + 40 * 3 / 4 verified contracts.
    assert factors[EvidenceDimension.AUDITS_TESTING].value == Decimal("60")
    for factor in bundle.factors:
        assert isinstance(factor.value, Decimal)
        assert factor.source is EvidenceSource.DEFISCAN
        assert factor.source_url == REVIEW_URL
        assert factor.observed_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        assert factor.rule


def test_missing_protocol_coverage_returns_explicit_unavailable_bundle():
    client, fetcher = make_client()

    bundle = client.get_review("morpho")

    assert fetcher.requested == [INDEX_URL]
    assert bundle.status is EvidenceStatus.UNAVAILABLE
    assert bundle.factors == ()
    assert bundle.observations == ()
    assert bundle.source is EvidenceSource.DEFISCAN
    assert bundle.source_url == f"{DEFISCAN_BASE_URL}/data/morpho/compiled-review.json"
    assert bundle.retrieved_at == RETRIEVED_AT
    assert bundle.source_updated_at is None
    assert "morpho" in bundle.unavailable_reason


def test_indexed_review_that_is_not_published_is_unavailable():
    client, _ = make_client({INDEX_URL: fixture_bytes("defiscan-index.json")})

    bundle = client.get_review("sample-protocol")

    assert bundle.status is EvidenceStatus.UNAVAILABLE
    assert bundle.factors == ()
    assert "not found" in bundle.unavailable_reason


def test_non_ethereum_review_is_unavailable_for_mainnet_analysis():
    client, fetcher = make_client()

    bundle = client.get_review("sample-base-vault")

    assert fetcher.requested == [INDEX_URL]
    assert bundle.status is EvidenceStatus.UNAVAILABLE
    assert "Base" in bundle.unavailable_reason


def test_review_for_a_different_project_is_rejected():
    responses = {
        INDEX_URL: fixture_bytes("defiscan-index.json"),
        REVIEW_URL: fixture_bytes("defiscan-review.json").replace(
            b'"project": "sample-protocol"', b'"project": "other-protocol"'
        ),
    }
    client, _ = make_client(responses)

    with pytest.raises(EvidenceFormatError):
        client.get_review("sample-protocol")


def test_review_without_dimension_sections_produces_no_factors():
    responses = {
        INDEX_URL: fixture_bytes("defiscan-index.json"),
        REVIEW_URL: b'{"project": "sample-protocol", "lastModified": "2026-09-01T12:00:00.000Z"}',
    }
    client, _ = make_client(responses)

    bundle = client.get_review("sample-protocol")

    assert bundle.status is EvidenceStatus.AVAILABLE
    assert bundle.factors == ()


def test_invalid_slug_is_rejected_before_fetching():
    client, fetcher = make_client()

    with pytest.raises(ValueError):
        client.get_review("../index")
    assert fetcher.requested == []


def test_urllib_fetch_maps_http_404_to_not_found(monkeypatch):
    def raise_404(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO())

    monkeypatch.setattr(defiscan.urllib.request, "urlopen", raise_404)

    with pytest.raises(EvidenceNotFoundError):
        urllib_fetch(REVIEW_URL)


def test_urllib_fetch_reports_other_failures_without_response_body(monkeypatch):
    def raise_500(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 500, "Server Error", {}, io.BytesIO(b"secret-body")
        )

    monkeypatch.setattr(defiscan.urllib.request, "urlopen", raise_500)

    with pytest.raises(DefiScanUnavailableError) as error:
        urllib_fetch(REVIEW_URL)
    assert "secret-body" not in str(error.value)
    assert "500" in str(error.value)
