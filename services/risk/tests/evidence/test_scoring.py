import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
)
from app.evidence.models import (
    CombinedEvidence,
    DimensionStatus,
    EvidenceBundle,
    EvidenceDimension,
    EvidenceFactor,
    EvidenceFormatError,
    EvidenceScore,
    EvidenceSource,
    EvidenceStatus,
    SourceAttribution,
)
from app.evidence.scoring import (
    DIMENSION_WEIGHTS,
    MODEL_VERSION,
    annual_central_probability,
    load_defisafety_evidence,
    map_score_to_probability,
    score_evidence,
)
from app.infrastructure.freshness import DataConflict, DataQuality, Freshness


ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
SNAPSHOT = FIXTURES / "defisafety-snapshot.json"
EVALUATED_AT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
RETRIEVED_AT = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)
ALL_DIMENSIONS = tuple(EvidenceDimension)


def make_bundle(
    source: EvidenceSource,
    values: dict[EvidenceDimension, str],
    *,
    updated_at: datetime | None = datetime(2026, 9, 1, tzinfo=UTC),
    protocol: str = "sample-protocol",
) -> EvidenceBundle:
    url = f"https://example.com/{source.value}/{protocol}"
    return EvidenceBundle(
        protocol=protocol,
        source=source,
        status=EvidenceStatus.AVAILABLE,
        source_url=url,
        methodology_url=f"https://example.com/{source.value}/methodology",
        retrieved_at=RETRIEVED_AT,
        source_updated_at=updated_at,
        factors=tuple(
            EvidenceFactor(
                dimension=dimension,
                value=Decimal(value),
                source=source,
                source_url=url,
                observed_at=updated_at,
                rule="test factor",
            )
            for dimension, value in values.items()
        ),
        observations=(),
    )


def combined(*bundles: EvidenceBundle) -> CombinedEvidence:
    return CombinedEvidence(
        protocol="sample-protocol", bundles=bundles, evaluated_at=EVALUATED_AT
    )


def complete_values(value: str) -> dict[EvidenceDimension, str]:
    return {dimension: value for dimension in ALL_DIMENSIONS}


def dimension(score: EvidenceScore, item: EvidenceDimension):
    return next(entry for entry in score.dimensions if entry.dimension is item)


def high_confidence_score(value: str) -> EvidenceScore:
    return EvidenceScore(
        protocol="sample-protocol",
        model_version=MODEL_VERSION,
        score=Decimal(value),
        confidence=ConfidenceLevel.HIGH,
        dimensions=(),
        quality=DataQuality(freshness=(Freshness.FRESH,), conflicts=(), critical_missing=()),
        attributions=(
            SourceAttribution(
                source=EvidenceSource.DEFISCAN,
                status=EvidenceStatus.AVAILABLE,
                source_url="https://example.com/defiscan/sample-protocol",
                methodology_url="https://example.com/defiscan/methodology",
                retrieved_at=RETRIEVED_AT,
                source_updated_at=datetime(2026, 9, 1, tzinfo=UTC),
                freshness=Freshness.FRESH,
                unavailable_reason=None,
            ),
        ),
        evaluated_at=EVALUATED_AT,
    )


def thirty_day(annual: str) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return Decimal(1) - (Decimal(1) - Decimal(annual)) ** (Decimal(30) / Decimal(365))


def test_dimension_weights_are_exactly_30_25_20_15_10():
    assert DIMENSION_WEIGHTS == {
        EvidenceDimension.AUDITS_TESTING: Decimal("0.30"),
        EvidenceDimension.CONTROLS_GOVERNANCE: Decimal("0.25"),
        EvidenceDimension.TRACK_RECORD: Decimal("0.20"),
        EvidenceDimension.DEPENDENCIES: Decimal("0.15"),
        EvidenceDimension.MONITORING_BOUNTY: Decimal("0.10"),
    }
    assert sum(DIMENSION_WEIGHTS.values()) == Decimal("1.00")
    assert all(isinstance(weight, Decimal) for weight in DIMENSION_WEIGHTS.values())


def test_complete_fresh_evidence_scores_weighted_sum_with_high_confidence():
    values = {
        EvidenceDimension.AUDITS_TESTING: "90",
        EvidenceDimension.CONTROLS_GOVERNANCE: "80",
        EvidenceDimension.TRACK_RECORD: "70",
        EvidenceDimension.DEPENDENCIES: "60",
        EvidenceDimension.MONITORING_BOUNTY: "50",
    }

    score = score_evidence(combined(make_bundle(EvidenceSource.DEFISCAN, values)))

    # 0.30*90 + 0.25*80 + 0.20*70 + 0.15*60 + 0.10*50
    assert score.score == Decimal("75.00")
    assert score.confidence is ConfidenceLevel.HIGH
    assert score.model_version == MODEL_VERSION
    assert score.quality.conflicts == ()
    assert score.quality.critical_missing == ()
    assert dimension(score, EvidenceDimension.AUDITS_TESTING).contribution == Decimal("27.00")
    assert all(entry.status is DimensionStatus.OBSERVED for entry in score.dimensions)


def test_missing_dimension_scores_zero_and_lowers_confidence():
    values = complete_values("90")
    del values[EvidenceDimension.TRACK_RECORD]

    score = score_evidence(combined(make_bundle(EvidenceSource.DEFISCAN, values)))

    track_record = dimension(score, EvidenceDimension.TRACK_RECORD)
    assert track_record.status is DimensionStatus.MISSING
    assert track_record.value is None
    assert track_record.contribution == Decimal("0")
    assert score.score == Decimal("72.00")
    assert score.confidence is ConfidenceLevel.MEDIUM
    assert "track_record_incidents" in score.quality.critical_missing


@pytest.mark.parametrize("hidden_value", ["0", "1", "50", "100"])
def test_missing_evidence_never_scores_better_than_present_evidence(hidden_value):
    present = complete_values("80")
    present[EvidenceDimension.CONTROLS_GOVERNANCE] = hidden_value
    missing = complete_values("80")
    del missing[EvidenceDimension.CONTROLS_GOVERNANCE]

    with_evidence = score_evidence(combined(make_bundle(EvidenceSource.DEFISCAN, present)))
    without_evidence = score_evidence(combined(make_bundle(EvidenceSource.DEFISCAN, missing)))

    assert without_evidence.score <= with_evidence.score


def test_conflicting_sources_are_preserved_and_conservative_value_is_scored():
    defiscan = make_bundle(
        EvidenceSource.DEFISCAN,
        {**complete_values("80"), EvidenceDimension.CONTROLS_GOVERNANCE: "30"},
    )
    defisafety = make_bundle(
        EvidenceSource.DEFISAFETY,
        {EvidenceDimension.CONTROLS_GOVERNANCE: "72"},
    )

    score = score_evidence(combined(defiscan, defisafety))

    controls = dimension(score, EvidenceDimension.CONTROLS_GOVERNANCE)
    assert controls.status is DimensionStatus.CONFLICTING
    assert controls.value == Decimal("30")
    assert controls.selected_source is EvidenceSource.DEFISCAN
    assert [(f.source, f.value) for f in controls.candidates] == [
        (EvidenceSource.DEFISCAN, Decimal("30")),
        (EvidenceSource.DEFISAFETY, Decimal("72")),
    ]
    assert score.quality.conflicts == (
        DataConflict(
            field="upgradeability_admin_governance",
            values=("30", "72"),
            sources=("defiscan", "defisafety"),
        ),
    )
    assert score.confidence is ConfidenceLevel.MEDIUM


def test_conflict_selection_does_not_depend_on_source_order():
    low = make_bundle(EvidenceSource.DEFISAFETY, complete_values("40"))
    high = make_bundle(EvidenceSource.DEFISCAN, complete_values("90"))

    first = score_evidence(combined(low, high))
    second = score_evidence(combined(high, low))

    assert first.score == second.score == Decimal("40.00")


def test_small_methodology_differences_use_conservative_value_without_conflict():
    defiscan = make_bundle(EvidenceSource.DEFISCAN, complete_values("80"))
    defisafety = make_bundle(
        EvidenceSource.DEFISAFETY, {EvidenceDimension.AUDITS_TESTING: "70.01"}
    )

    score = score_evidence(combined(defiscan, defisafety))

    audits = dimension(score, EvidenceDimension.AUDITS_TESTING)
    assert audits.status is DimensionStatus.OBSERVED
    assert audits.value == Decimal("70.01")
    assert score.quality.conflicts == ()
    assert score.confidence is ConfidenceLevel.HIGH


def test_stale_evidence_is_used_with_lower_confidence():
    stale_bundle = make_bundle(
        EvidenceSource.DEFISCAN,
        complete_values("90"),
        updated_at=EVALUATED_AT - timedelta(days=200),
    )

    score = score_evidence(combined(stale_bundle))

    assert score.score == Decimal("90.00")
    assert all(entry.status is DimensionStatus.STALE for entry in score.dimensions)
    assert score.confidence is ConfidenceLevel.LOW
    assert score.attributions[0].freshness is Freshness.STALE_USABLE


def test_evidence_without_source_update_time_is_treated_as_stale():
    bundle = make_bundle(EvidenceSource.DEFISCAN, complete_values("90"), updated_at=None)

    score = score_evidence(combined(bundle))

    assert score.attributions[0].freshness is Freshness.STALE_USABLE
    assert score.confidence is ConfidenceLevel.LOW


def test_expired_evidence_is_excluded_and_reported_missing():
    expired = make_bundle(
        EvidenceSource.DEFISAFETY,
        complete_values("95"),
        updated_at=EVALUATED_AT - timedelta(days=400),
    )
    current = make_bundle(
        EvidenceSource.DEFISCAN, {EvidenceDimension.AUDITS_TESTING: "60"}
    )

    score = score_evidence(combined(expired, current))

    assert score.score == Decimal("18.00")
    controls = dimension(score, EvidenceDimension.CONTROLS_GOVERNANCE)
    assert controls.status is DimensionStatus.MISSING
    assert [(f.source, f.value) for f in controls.candidates] == [
        (EvidenceSource.DEFISAFETY, Decimal("95"))
    ]
    assert score.confidence is ConfidenceLevel.LOW
    assert Freshness.EXPIRED in score.quality.freshness


def test_no_usable_evidence_is_unavailable_not_zero_risk():
    unavailable = EvidenceBundle.unavailable(
        protocol="sample-protocol",
        source=EvidenceSource.DEFISCAN,
        source_url="https://example.com/defiscan/sample-protocol",
        methodology_url="https://example.com/defiscan/methodology",
        retrieved_at=RETRIEVED_AT,
        reason="not covered",
    )

    score = score_evidence(combined(unavailable))
    estimate = map_score_to_probability(score)

    assert score.score is None
    assert score.confidence is ConfidenceLevel.UNAVAILABLE
    assert len(score.quality.critical_missing) == len(ALL_DIMENSIONS)
    assert estimate.method is ProbabilityMethod.UNAVAILABLE
    assert (estimate.low, estimate.central, estimate.high) == (None, None, None)
    assert estimate.confidence is ConfidenceLevel.UNAVAILABLE
    assert "not covered" in estimate.explanation


def test_combined_evidence_rejects_bundles_for_another_protocol():
    with pytest.raises(ValueError):
        CombinedEvidence(
            protocol="sample-protocol",
            bundles=(make_bundle(EvidenceSource.DEFISCAN, {}, protocol="other"),),
            evaluated_at=EVALUATED_AT,
        )


def test_evidence_factor_rejects_binary_floats_and_out_of_range_values():
    for bad_value in (90.0, Decimal("-1"), Decimal("100.01"), Decimal("NaN")):
        with pytest.raises((TypeError, ValueError)):
            EvidenceFactor(
                dimension=EvidenceDimension.AUDITS_TESTING,
                value=bad_value,
                source=EvidenceSource.DEFISCAN,
                source_url="https://example.com",
                observed_at=None,
                rule="test",
            )


@pytest.mark.parametrize(
    ("score", "annual"),
    [
        ("100", "0.005"),
        ("85", "0.005"),
        ("84", "0.01"),
        ("84.99", "0.01"),
        ("70", "0.01"),
        ("69", "0.03"),
        ("50", "0.03"),
        ("49", "0.08"),
        ("0", "0.08"),
    ],
)
def test_score_bands_map_to_annual_central_probability(score, annual):
    assert annual_central_probability(Decimal(score)) == Decimal(annual)


@pytest.mark.parametrize(
    ("score", "annual"),
    [("85", "0.005"), ("84", "0.01"), ("70", "0.01"), ("69", "0.03"), ("50", "0.03"), ("49", "0.08")],
)
def test_thirty_day_conversion_and_half_to_twice_range(score, annual):
    estimate = map_score_to_probability(high_confidence_score(score))

    assert isinstance(estimate, ProbabilityEstimate)
    assert estimate.method is ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL
    assert estimate.horizon_days == 30
    assert estimate.central == thirty_day(annual)
    assert estimate.low == estimate.central / 2
    assert estimate.high == estimate.central * 2
    assert Decimal(0) <= estimate.low <= estimate.central <= estimate.high <= Decimal(1)
    assert all(isinstance(value, Decimal) for value in (estimate.low, estimate.central, estimate.high))
    assert estimate.confidence is ConfidenceLevel.HIGH
    assert estimate.freshness is FreshnessStatus.FRESH
    assert estimate.retrieved_at == RETRIEVED_AT
    assert "defiscan" in estimate.source
    assert MODEL_VERSION in estimate.explanation
    assert "model-estimated" in estimate.explanation
    assert "empirical" not in estimate.method.value


def test_thirty_day_conversion_matches_known_value():
    estimate = map_score_to_probability(high_confidence_score("85"))

    # 1 - 0.995 ** (30 / 365) ~= 0.000411944
    assert Decimal("0.0004119") < estimate.central < Decimal("0.0004120")


def test_lower_confidence_widens_probability_range():
    high = map_score_to_probability(high_confidence_score("60"))
    medium_score = high_confidence_score("60")
    medium = map_score_to_probability(
        replace(medium_score, confidence=ConfidenceLevel.MEDIUM)
    )
    low = map_score_to_probability(replace(medium_score, confidence=ConfidenceLevel.LOW))

    assert high.central == medium.central == low.central
    assert low.low < medium.low < high.low
    assert high.high < medium.high < low.high
    assert medium.low == medium.central / 3
    assert medium.high == medium.central * 3
    assert low.low == low.central / 4
    assert low.high == low.central * 4


def test_probability_mapping_rejects_unsupported_horizon():
    with pytest.raises(ValueError):
        map_score_to_probability(high_confidence_score("85"), horizon_days=31)


def test_probability_estimate_is_labelled_model_estimated_in_json():
    estimate = map_score_to_probability(high_confidence_score("85"))

    payload = json.loads(estimate.model_dump_json())

    assert payload["method"] == "model_estimated_protocol"
    assert isinstance(payload["central"], str)


def test_defisafety_snapshot_maps_answers_to_dimensions():
    bundle = load_defisafety_evidence(SNAPSHOT, "sample-protocol")

    assert bundle.status is EvidenceStatus.AVAILABLE
    assert bundle.source is EvidenceSource.DEFISAFETY
    assert bundle.source_url == "https://example.com/defisafety/sample-protocol"
    assert bundle.methodology_url == "https://defisafety.com/documentation-09/"
    assert bundle.retrieved_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert bundle.source_updated_at == datetime(2026, 6, 30, tzinfo=UTC)
    factors = {factor.dimension: factor.value for factor in bundle.factors}
    assert factors == {
        # mean(Q10..Q15) = 440 / 6, floored to cents.
        EvidenceDimension.AUDITS_TESTING: Decimal("73.33"),
        EvidenceDimension.CONTROLS_GOVERNANCE: Decimal("72.00"),
        EvidenceDimension.DEPENDENCIES: Decimal("75.00"),
        EvidenceDimension.MONITORING_BOUNTY: Decimal("50.00"),
    }
    preserved = {item.label: item.value for item in bundle.observations}
    assert preserved["overall_score"] == "81"
    assert preserved["methodology_version"] == "0.9"
    assert preserved["Q3"] == "50"


def test_defisafety_partial_answers_count_missing_questions_as_zero():
    bundle = load_defisafety_evidence(SNAPSHOT, "partial-protocol")

    factors = {factor.dimension: factor for factor in bundle.factors}
    # Q10=100, Q11=80, Q12..Q15 missing -> 180 / 6.
    assert factors[EvidenceDimension.AUDITS_TESTING].value == Decimal("30.00")
    assert "Q12" in factors[EvidenceDimension.AUDITS_TESTING].rule
    assert factors[EvidenceDimension.MONITORING_BOUNTY].value == Decimal("100.00")
    assert EvidenceDimension.CONTROLS_GOVERNANCE not in factors
    assert EvidenceDimension.DEPENDENCIES not in factors


def test_defisafety_uncaptured_and_absent_protocols_are_unavailable():
    uncaptured = load_defisafety_evidence(SNAPSHOT, "uncaptured-protocol")
    absent = load_defisafety_evidence(SNAPSHOT, "absent-protocol")

    for bundle in (uncaptured, absent):
        assert bundle.status is EvidenceStatus.UNAVAILABLE
        assert bundle.factors == ()
        assert bundle.source is EvidenceSource.DEFISAFETY
        assert bundle.unavailable_reason
    assert uncaptured.retrieved_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    assert absent.retrieved_at == datetime(2026, 10, 1, tzinfo=UTC)


def test_defisafety_snapshot_rejects_out_of_range_answers(tmp_path):
    snapshot = json.loads(SNAPSHOT.read_text())
    snapshot["protocols"][0]["answers"]["Q14"] = "140"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))

    with pytest.raises(EvidenceFormatError):
        load_defisafety_evidence(path, "sample-protocol")


def test_version_controlled_defisafety_snapshot_has_no_unattributed_scores():
    path = ROOT / "data" / "evidence" / "defisafety.ethereum.json"

    for protocol in ("morpho", "uniswap-v3"):
        bundle = load_defisafety_evidence(path, protocol)
        assert bundle.source_url.startswith("https://")
        assert bundle.retrieved_at.tzinfo is not None
        if bundle.status is EvidenceStatus.AVAILABLE:
            assert bundle.source_updated_at is not None
            assert bundle.factors
        else:
            assert bundle.factors == ()
            assert bundle.unavailable_reason


def test_end_to_end_fixture_evidence_is_conservative():
    defisafety = load_defisafety_evidence(SNAPSHOT, "sample-protocol")
    defiscan = make_bundle(
        EvidenceSource.DEFISCAN,
        {
            EvidenceDimension.AUDITS_TESTING: "60",
            EvidenceDimension.CONTROLS_GOVERNANCE: "30",
            EvidenceDimension.DEPENDENCIES: "55",
        },
    )

    score = score_evidence(combined(defiscan, defisafety))
    estimate = map_score_to_probability(score)

    # 0.30*60 + 0.25*30 + 0.20*0 (missing) + 0.15*55 + 0.10*50
    assert score.score == Decimal("38.75")
    assert dimension(score, EvidenceDimension.DEPENDENCIES).status is DimensionStatus.CONFLICTING
    assert dimension(score, EvidenceDimension.AUDITS_TESTING).status is DimensionStatus.OBSERVED
    assert score.confidence is ConfidenceLevel.LOW
    assert estimate.central == thirty_day("0.08")
    assert estimate.low == estimate.central / 4
    assert estimate.confidence is ConfidenceLevel.LOW
