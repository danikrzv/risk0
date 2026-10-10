"""Transparent protocol-evidence scoring and model-estimated probabilities.

The score-to-probability mapping is the spec's demonstration heuristic, not an
actuarial or empirical estimate. Missing, stale, expired, or conflicting
evidence can only lower the score or widen the range; it never improves it.
"""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from pathlib import Path

from app.contracts.models import (
    ConfidenceLevel,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
)
from app.evidence.models import (
    CombinedEvidence,
    DimensionScore,
    DimensionStatus,
    EvidenceBundle,
    EvidenceDimension,
    EvidenceFactor,
    EvidenceFormatError,
    EvidenceObservation,
    EvidenceScore,
    EvidenceSource,
    EvidenceStatus,
    SourceAttribution,
    parse_timestamp,
)
from app.infrastructure.freshness import DataConflict, DataQuality, Freshness


MODEL_VERSION = "protocol-evidence-v1"
SUPPORTED_HORIZON_DAYS = 30
DAYS_PER_YEAR = 365
PRECISION = 28

DIMENSION_WEIGHTS: Mapping[EvidenceDimension, Decimal] = {
    EvidenceDimension.AUDITS_TESTING: Decimal("0.30"),
    EvidenceDimension.CONTROLS_GOVERNANCE: Decimal("0.25"),
    EvidenceDimension.TRACK_RECORD: Decimal("0.20"),
    EvidenceDimension.DEPENDENCIES: Decimal("0.15"),
    EvidenceDimension.MONITORING_BOUNTY: Decimal("0.10"),
}

# (minimum score, annual central impairment probability), highest band first.
SCORE_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("85"), Decimal("0.005")),
    (Decimal("70"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.03")),
    (Decimal("0"), Decimal("0.08")),
)

# Sources whose values for one dimension differ by at least this many points
# are reported as conflicting. Smaller gaps are methodology noise, but the
# lower value is still the one scored.
CONFLICT_THRESHOLD = Decimal("20")

# Evidence content older than EVIDENCE_FRESH_FOR is stale; older than
# EVIDENCE_MAXIMUM_AGE it is expired and excluded from the score.
EVIDENCE_FRESH_FOR = timedelta(days=180)
EVIDENCE_MAXIMUM_AGE = timedelta(days=365)

# Divisor and multiplier applied to the central estimate. HIGH is the spec's
# initial half-to-twice range; lower confidence widens it.
RANGE_FACTORS: Mapping[ConfidenceLevel, Decimal] = {
    ConfidenceLevel.HIGH: Decimal(2),
    ConfidenceLevel.MEDIUM: Decimal(3),
    ConfidenceLevel.LOW: Decimal(4),
}

SCORE_QUANTUM = Decimal("0.01")
FRESHNESS_ORDER = (Freshness.FRESH, Freshness.STALE_USABLE, Freshness.EXPIRED)

DEFISAFETY_QUESTIONS: Mapping[EvidenceDimension, tuple[str, ...]] = {
    EvidenceDimension.AUDITS_TESTING: ("Q10", "Q11", "Q12", "Q13", "Q14", "Q15"),
    EvidenceDimension.CONTROLS_GOVERNANCE: ("Q19", "Q20", "Q21", "Q22", "Q23"),
    EvidenceDimension.DEPENDENCIES: ("Q25", "Q26"),
    EvidenceDimension.MONITORING_BOUNTY: ("Q16", "Q17", "Q18"),
}
NOT_APPLICABLE = "n/a"


def score_evidence(bundle: CombinedEvidence) -> EvidenceScore:
    attributions = tuple(
        _attribution(item, bundle.evaluated_at) for item in bundle.bundles
    )
    freshness_by_bundle = {
        id(item): attribution.freshness
        for item, attribution in zip(bundle.bundles, attributions)
    }

    dimensions: list[DimensionScore] = []
    conflicts: list[DataConflict] = []
    for dimension, weight in DIMENSION_WEIGHTS.items():
        candidates: list[EvidenceFactor] = []
        usable: list[tuple[EvidenceFactor, Freshness]] = []
        for item in bundle.bundles:
            freshness = freshness_by_bundle[id(item)]
            for factor in item.factors:
                if factor.dimension is not dimension:
                    continue
                candidates.append(factor)
                if freshness is not Freshness.EXPIRED:
                    usable.append((factor, freshness))
        entry, conflict = _score_dimension(dimension, weight, tuple(candidates), usable)
        dimensions.append(entry)
        if conflict is not None:
            conflicts.append(conflict)

    observed = [entry for entry in dimensions if entry.status is not DimensionStatus.MISSING]
    missing = tuple(
        entry.dimension.value
        for entry in dimensions
        if entry.status is DimensionStatus.MISSING
    )
    if observed:
        with localcontext() as context:
            context.prec = PRECISION
            total = sum((entry.contribution for entry in dimensions), Decimal(0))
            score: Decimal | None = total.quantize(SCORE_QUANTUM, rounding=ROUND_FLOOR)
        confidence = _confidence(dimensions)
    else:
        score = None
        confidence = ConfidenceLevel.UNAVAILABLE

    quality = DataQuality(
        freshness=tuple(
            attribution.freshness
            for attribution in attributions
            if attribution.status is EvidenceStatus.AVAILABLE
        ),
        conflicts=tuple(conflicts),
        critical_missing=missing,
    )
    return EvidenceScore(
        protocol=bundle.protocol,
        model_version=MODEL_VERSION,
        score=score,
        confidence=confidence,
        dimensions=tuple(dimensions),
        quality=quality,
        attributions=attributions,
        evaluated_at=bundle.evaluated_at,
    )


def annual_central_probability(score: Decimal) -> Decimal:
    if not isinstance(score, Decimal) or not score.is_finite():
        raise ValueError("score must be a finite Decimal")
    if not Decimal(0) <= score <= Decimal(100):
        raise ValueError("score must be within 0-100")
    for minimum, probability in SCORE_BANDS:
        if score >= minimum:
            return probability
    raise AssertionError("score bands must cover 0-100")


def map_score_to_probability(
    score: EvidenceScore, horizon_days: int = SUPPORTED_HORIZON_DAYS
) -> ProbabilityEstimate:
    if horizon_days != SUPPORTED_HORIZON_DAYS:
        raise ValueError("only the 30-day horizon is supported")

    usable = tuple(
        attribution
        for attribution in score.attributions
        if attribution.status is EvidenceStatus.AVAILABLE
        and attribution.freshness is not Freshness.EXPIRED
    )
    if score.score is None or score.confidence is ConfidenceLevel.UNAVAILABLE or not usable:
        return _unavailable_estimate(score)

    annual = annual_central_probability(score.score)
    factor = RANGE_FACTORS[score.confidence]
    with localcontext() as context:
        context.prec = PRECISION
        exponent = Decimal(horizon_days) / Decimal(DAYS_PER_YEAR)
        central = Decimal(1) - (Decimal(1) - annual) ** exponent
        low = max(Decimal(0), central / factor)
        high = min(Decimal(1), central * factor)

    worst = max((attribution.freshness for attribution in usable), key=FRESHNESS_ORDER.index)
    return ProbabilityEstimate(
        method=ProbabilityMethod.MODEL_ESTIMATED_PROTOCOL,
        low=low,
        central=central,
        high=high,
        confidence=score.confidence,
        horizon_days=SUPPORTED_HORIZON_DAYS,
        source=_source_label(usable),
        retrieved_at=min(attribution.retrieved_at for attribution in usable),
        freshness=FreshnessStatus(worst.value),
        explanation=_explanation(score, annual, factor),
    )


def load_defisafety_evidence(path: Path, protocol: str) -> EvidenceBundle:
    try:
        snapshot = json.loads(path.read_text(), parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise EvidenceFormatError(f"invalid DeFiSafety snapshot: {error.msg}") from error
    if not isinstance(snapshot, dict) or snapshot.get("source") != EvidenceSource.DEFISAFETY:
        raise EvidenceFormatError("snapshot is not a DeFiSafety evidence snapshot")

    methodology_url = _required_str(snapshot, "methodology_url")
    methodology_version = _required_str(snapshot, "methodology_version")
    compiled_at = parse_timestamp(_required_str(snapshot, "snapshot_compiled_at"))
    entries = snapshot.get("protocols")
    if not isinstance(entries, list):
        raise EvidenceFormatError("snapshot protocols must be a list")

    entry = next(
        (item for item in entries if isinstance(item, dict) and item.get("protocol") == protocol),
        None,
    )
    if entry is None:
        return EvidenceBundle.unavailable(
            protocol=protocol,
            source=EvidenceSource.DEFISAFETY,
            source_url=methodology_url,
            methodology_url=methodology_url,
            retrieved_at=compiled_at,
            reason=f"DeFiSafety snapshot has no entry for {protocol!r}",
        )

    if entry.get("status") != "reviewed":
        return EvidenceBundle.unavailable(
            protocol=protocol,
            source=EvidenceSource.DEFISAFETY,
            source_url=_required_str(entry, "source_url"),
            methodology_url=methodology_url,
            retrieved_at=parse_timestamp(_required_str(entry, "checked_at")),
            reason=_required_str(entry, "note"),
        )

    review_url = _required_str(entry, "review_url")
    review_date = parse_timestamp(_required_str(entry, "review_date"))
    answers = entry.get("answers")
    if not isinstance(answers, dict):
        raise EvidenceFormatError("reviewed DeFiSafety entries require answers")
    parsed = {str(key): _parse_answer(key, value) for key, value in answers.items()}

    factors = []
    for dimension, questions in DEFISAFETY_QUESTIONS.items():
        factor = _defisafety_factor(dimension, questions, parsed, review_url, review_date)
        if factor is not None:
            factors.append(factor)

    observations = [
        EvidenceObservation("metadata", "methodology_version", methodology_version),
    ]
    if "overall_score" in entry:
        observations.append(
            EvidenceObservation(
                "metadata", "overall_score", str(entry["overall_score"]), review_date, review_url
            )
        )
    observations.extend(
        EvidenceObservation(
            "answer",
            key,
            NOT_APPLICABLE if value is None else str(value),
            review_date,
            review_url,
        )
        for key, value in parsed.items()
    )

    return EvidenceBundle(
        protocol=protocol,
        source=EvidenceSource.DEFISAFETY,
        status=EvidenceStatus.AVAILABLE,
        source_url=review_url,
        methodology_url=methodology_url,
        retrieved_at=parse_timestamp(_required_str(entry, "retrieved_at")),
        source_updated_at=review_date,
        factors=tuple(factors),
        observations=tuple(observations),
    )


def _attribution(bundle: EvidenceBundle, evaluated_at: datetime) -> SourceAttribution:
    if bundle.status is EvidenceStatus.UNAVAILABLE:
        freshness = Freshness.EXPIRED
    elif bundle.source_updated_at is None:
        # Unknown content age is never treated as fresh.
        freshness = Freshness.STALE_USABLE
    else:
        freshness = Freshness.from_times(
            bundle.source_updated_at,
            evaluated_at,
            EVIDENCE_FRESH_FOR,
            EVIDENCE_MAXIMUM_AGE,
        )
    return SourceAttribution(
        source=bundle.source,
        status=bundle.status,
        source_url=bundle.source_url,
        methodology_url=bundle.methodology_url,
        retrieved_at=bundle.retrieved_at,
        source_updated_at=bundle.source_updated_at,
        freshness=freshness,
        unavailable_reason=bundle.unavailable_reason,
    )


def _score_dimension(
    dimension: EvidenceDimension,
    weight: Decimal,
    candidates: tuple[EvidenceFactor, ...],
    usable: list[tuple[EvidenceFactor, Freshness]],
) -> tuple[DimensionScore, DataConflict | None]:
    if not usable:
        return (
            DimensionScore(
                dimension=dimension,
                weight=weight,
                status=DimensionStatus.MISSING,
                value=None,
                contribution=Decimal(0),
                selected_source=None,
                candidates=candidates,
            ),
            None,
        )

    # Conservative choice: the lowest factor value, ties broken by source name
    # so the result does not depend on bundle order.
    selected, _ = min(usable, key=lambda item: (item[0].value, item[0].source.value))
    values = [factor.value for factor, _ in usable]
    conflict = None
    if max(values) - min(values) >= CONFLICT_THRESHOLD:
        status = DimensionStatus.CONFLICTING
        conflict = DataConflict(
            field=dimension.value,
            values=tuple(str(factor.value) for factor, _ in usable),
            sources=tuple(factor.source.value for factor, _ in usable),
        )
    elif any(freshness is not Freshness.FRESH for _, freshness in usable):
        status = DimensionStatus.STALE
    else:
        status = DimensionStatus.OBSERVED

    with localcontext() as context:
        context.prec = PRECISION
        contribution = weight * selected.value
    return (
        DimensionScore(
            dimension=dimension,
            weight=weight,
            status=status,
            value=selected.value,
            contribution=contribution,
            selected_source=selected.source,
            candidates=candidates,
        ),
        conflict,
    )


def _confidence(dimensions: list[DimensionScore]) -> ConfidenceLevel:
    issues = sum(1 for entry in dimensions if entry.status is not DimensionStatus.OBSERVED)
    if issues == 0:
        return ConfidenceLevel.HIGH
    if issues == 1:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def _unavailable_estimate(score: EvidenceScore) -> ProbabilityEstimate:
    reasons = [
        f"{attribution.source.value}: {attribution.unavailable_reason}"
        for attribution in score.attributions
        if attribution.unavailable_reason
    ]
    expired = [
        attribution.source.value
        for attribution in score.attributions
        if attribution.status is EvidenceStatus.AVAILABLE
        and attribution.freshness is Freshness.EXPIRED
    ]
    if expired:
        reasons.append(f"expired evidence from {', '.join(expired)}")
    detail = "; ".join(reasons) if reasons else "no protocol evidence supplied"
    return ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE,
        low=None,
        central=None,
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE,
        horizon_days=SUPPORTED_HORIZON_DAYS,
        source=_source_label(score.attributions) if score.attributions else "none",
        retrieved_at=min(
            (attribution.retrieved_at for attribution in score.attributions),
            default=score.evaluated_at,
        ),
        freshness=FreshnessStatus.EXPIRED,
        explanation=(
            f"Protocol impairment probability unavailable for {score.protocol} "
            f"({MODEL_VERSION}): {detail}."
        ),
    )


def _source_label(attributions: tuple[SourceAttribution, ...]) -> str:
    return "; ".join(
        f"{attribution.source.value} {attribution.source_url}" for attribution in attributions
    )


def _explanation(score: EvidenceScore, annual: Decimal, factor: Decimal) -> str:
    breakdown = ", ".join(
        f"{entry.dimension.value}={entry.value if entry.value is not None else 'missing(0)'}"
        f"x{entry.weight} [{entry.status.value}]"
        for entry in score.dimensions
    )
    return (
        f"model-estimated protocol impairment probability ({MODEL_VERSION}); "
        f"demonstration heuristic, not an empirical or actuarial estimate. "
        f"Evidence score {score.score} maps to annual central {annual}, converted "
        f"to 30 days as 1 - (1 - annual)^(30/365); range is central / {factor} "
        f"to central x {factor} for {score.confidence.value} confidence. "
        f"Factors: {breakdown}."
    )


def _defisafety_factor(
    dimension: EvidenceDimension,
    questions: tuple[str, ...],
    answers: Mapping[str, Decimal | None],
    review_url: str,
    review_date: datetime,
) -> EvidenceFactor | None:
    present = [question for question in questions if question in answers]
    applicable = [question for question in present if answers[question] is not None]
    if not applicable:
        return None
    absent = [question for question in questions if question not in answers]
    counted = [question for question in questions if answers.get(question, Decimal(0)) is not None]
    with localcontext() as context:
        context.prec = PRECISION
        total = sum((answers.get(question) or Decimal(0) for question in counted), Decimal(0))
        value = (total / Decimal(len(counted))).quantize(SCORE_QUANTUM, rounding=ROUND_FLOOR)
    rule = f"mean of DeFiSafety {', '.join(counted)}"
    if absent:
        rule += f"; unanswered {', '.join(absent)} counted as 0"
    return EvidenceFactor(
        dimension=dimension,
        value=value,
        source=EvidenceSource.DEFISAFETY,
        source_url=review_url,
        observed_at=review_date,
        rule=rule,
    )


def _parse_answer(key: object, value: object) -> Decimal | None:
    if isinstance(value, str) and value.strip().lower() == NOT_APPLICABLE:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise EvidenceFormatError(f"DeFiSafety answer {key} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise EvidenceFormatError(f"DeFiSafety answer {key} is not a number") from error
    if not parsed.is_finite() or not Decimal(0) <= parsed <= Decimal(100):
        raise EvidenceFormatError(f"DeFiSafety answer {key} must be within 0-100")
    return parsed


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise EvidenceFormatError(f"missing required field {key!r}")
    return value
