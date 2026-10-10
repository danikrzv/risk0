"""Hybrid STAY / EXIT / UNAVAILABLE recommendation (spec § 13).

    expected_staying_loss_30d = sum(event_probability_30d * event_impact)

    if critical_data_missing:              UNAVAILABLE
    elif any_hard_safety_trigger:          EXIT
    elif expected_loss_incomplete:         UNAVAILABLE
    elif expected_loss > exit_cost_now:    EXIT
    else:                                  STAY       (equal values stay)

Critical data is missing when `data_quality.critical_missing` is non-empty, when
the exit estimate is EXPIRED (an expired quote cannot support a decision), when
any scenario reports `stressed_values["impact_complete"] is False` (missing,
expired, or unmodelled inputs), or when a scenario's impact is UNAVAILABLE with
no completeness flag at all. Missing data can never become STAY.

Hard triggers come from the scenarios (borrower liquidation, live oracle breach,
lender withdrawal shortfall) and from `live_triggers` the orchestrator observed
in current data regardless of the selected scenarios (live oracle breach,
protocol emergency). A hard trigger is a deterministic consequence of current
data or of a selected stress, so it does not need a probability.

The expected loss is incomplete when a scenario with a non-zero impact has no
central probability: an unknown probability is never treated as zero.

No double counting: `impact` already excludes the liquidity shortfall (it is
reported but is not a realized loss), and a scenario id selected more than once
(for example at two severities) contributes once, with its largest expected
loss, because the selections describe the same event.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.contracts.models import (
    ConfidenceLevel,
    ExitEstimate,
    FreshnessStatus,
    Recommendation,
    RecommendationDecision,
    ScenarioResult,
)
from app.domain.exit import NUMERAIRE_PLACES
from app.infrastructure.freshness import DataQuality


ZERO = Decimal(0)
IMPACT_COMPLETE_KEY = "impact_complete"
CONFIDENCE_ORDER = (
    ConfidenceLevel.UNAVAILABLE,
    ConfidenceLevel.LOW,
    ConfidenceLevel.MEDIUM,
    ConfidenceLevel.HIGH,
)
LIVE_ORACLE_FAILURE = "live:oracle_failure"
PROTOCOL_EMERGENCY = "live:protocol_emergency"


@dataclass(frozen=True, slots=True)
class _Contribution:
    scenario: ScenarioResult
    central: Decimal | None
    low: Decimal | None
    high: Decimal | None


def make_recommendation(
    scenarios: tuple[ScenarioResult, ...],
    exit_estimate: ExitEstimate,
    data_quality: DataQuality,
    *,
    live_triggers: Iterable[str] = (),
    numeraire: str = "",
) -> Recommendation:
    unit = f" {numeraire}" if numeraire else ""
    exit_cost = exit_estimate.total_cost
    contributions = _deduplicated(scenarios)
    known = [item for item in contributions if item.central is not None]
    expected_loss = _quantize(sum((item.central for item in known if item.central is not None), ZERO))

    incomplete = tuple(
        item.scenario.scenario_id
        for item in contributions
        if _impact_incomplete(item.scenario)
    )
    exit_expired = exit_estimate.freshness is FreshnessStatus.EXPIRED
    if data_quality.critical_missing or incomplete or exit_expired:
        missing = [*data_quality.critical_missing]
        if exit_expired:
            missing.append("a current exit estimate (the quote or snapshot is expired)")
        missing.extend(f"complete impact for {scenario_id}" for scenario_id in incomplete)
        return Recommendation(
            decision=RecommendationDecision.UNAVAILABLE,
            expected_loss=expected_loss,
            expected_loss_low=None,
            expected_loss_high=None,
            exit_cost=exit_cost,
            triggers=(),
            dominant_dependency=_dominant(contributions),
            confidence=ConfidenceLevel.UNAVAILABLE,
            explanation=(
                "Recommendation unavailable: critical data is missing or outside its "
                f"maximum age ({', '.join(dict.fromkeys(missing))}). Missing data is never "
                "treated as zero risk, so no STAY or EXIT is given."
            ),
        )

    triggers = _triggers(scenarios, live_triggers)
    unknown_probability = tuple(
        item.scenario.scenario_id
        for item in contributions
        if item.central is None and item.scenario.impact != ZERO
    )
    loss_low = None if unknown_probability else _quantize(_bound(known, "low"))
    loss_high = None if unknown_probability else _quantize(_bound(known, "high"))
    dominant = _dominant(contributions, triggered=bool(triggers))
    conflicts = data_quality.conflicts
    comparison = (
        f"expected 30-day staying loss {expected_loss}{unit}"
        + (f" (range {loss_low} to {loss_high}{unit})" if loss_low is not None else "")
        + f" versus exit cost now {exit_cost}{unit}"
    )

    if triggers:
        decision = RecommendationDecision.EXIT
        confidence = _lowest(
            [
                *(item.scenario.confidence for item in contributions if item.scenario.hard_triggers),
                _exit_confidence(exit_estimate),
            ]
        )
        parts = [
            f"EXIT: hard safety trigger{'s' if len(triggers) > 1 else ''} "
            f"{', '.join(triggers)} fired; this overrides the expected-loss comparison "
            f"({comparison})."
        ]
    elif unknown_probability:
        return Recommendation(
            decision=RecommendationDecision.UNAVAILABLE,
            expected_loss=expected_loss,
            expected_loss_low=None,
            expected_loss_high=None,
            exit_cost=exit_cost,
            triggers=(),
            dominant_dependency=dominant,
            confidence=ConfidenceLevel.UNAVAILABLE,
            explanation=(
                "Recommendation unavailable: no 30-day probability is available for "
                f"{', '.join(dict.fromkeys(unknown_probability))}, which has a non-zero "
                "impact, so the expected staying loss cannot be computed. An unknown "
                "probability is never treated as zero."
            ),
        )
    else:
        relevant = [
            item.scenario.confidence
            for item in contributions
            if item.scenario.impact != ZERO
        ]
        confidence = _lowest([*relevant, _exit_confidence(exit_estimate)])
        if expected_loss > exit_cost:
            decision = RecommendationDecision.EXIT
            parts = [f"EXIT: the {comparison}; the expected loss is higher."]
        else:
            decision = RecommendationDecision.STAY
            parts = [f"STAY: the {comparison}; the expected loss does not exceed the exit cost."]

    if dominant is not None:
        parts.append(f"Dominant dependency: {dominant}.")
    if conflicts:
        confidence = _lower(confidence)
        fields = ", ".join(dict.fromkeys(conflict.field for conflict in conflicts))
        parts.append(
            f"Sources conflict on {fields}; the conservative value was used and "
            "confidence was lowered."
        )
    if decision is RecommendationDecision.EXIT and not exit_estimate.withdrawable:
        parts.append(
            "Exit is recommended, but a full exit is currently unavailable: current "
            "liquidity or the quoted route cannot satisfy the whole position right now."
        )
    parts.append(f"Confidence: {confidence.value}.")
    return Recommendation(
        decision=decision,
        expected_loss=expected_loss,
        expected_loss_low=loss_low,
        expected_loss_high=loss_high,
        exit_cost=exit_cost,
        triggers=triggers,
        dominant_dependency=dominant,
        confidence=confidence,
        explanation=" ".join(parts),
    )


def _deduplicated(scenarios: Iterable[ScenarioResult]) -> list[_Contribution]:
    """One contribution per scenario id: the most conservative selection wins."""
    chosen: dict[str, _Contribution] = {}
    for scenario in scenarios:
        probability = scenario.probability
        central = None if probability.central is None else probability.central * scenario.impact
        low = probability.low if probability.low is not None else probability.central
        high = probability.high if probability.high is not None else probability.central
        item = _Contribution(
            scenario=scenario,
            central=central,
            low=None if low is None else low * scenario.impact,
            high=None if high is None else high * scenario.impact,
        )
        current = chosen.get(scenario.scenario_id)
        if current is None or _more_conservative(item, current):
            chosen[scenario.scenario_id] = item
    return list(chosen.values())


def _more_conservative(candidate: _Contribution, current: _Contribution) -> bool:
    # Unknown beats known (it blocks the decision); otherwise the larger loss wins.
    if candidate.central is None or current.central is None:
        return candidate.central is None and current.central is not None
    if _impact_incomplete(candidate.scenario) != _impact_incomplete(current.scenario):
        return _impact_incomplete(candidate.scenario)
    if candidate.central != current.central:
        return candidate.central > current.central
    return bool(candidate.scenario.hard_triggers) and not current.scenario.hard_triggers


def _impact_incomplete(scenario: ScenarioResult) -> bool:
    flag = scenario.stressed_values.get(IMPACT_COMPLETE_KEY)
    if flag is None:
        # Older producers without the flag: an UNAVAILABLE scenario with a known
        # probability can only be unavailable because of its data.
        return (
            scenario.confidence is ConfidenceLevel.UNAVAILABLE
            and scenario.probability.confidence is not ConfidenceLevel.UNAVAILABLE
        )
    return flag is False


def _triggers(scenarios: Iterable[ScenarioResult], live: Iterable[str]) -> tuple[str, ...]:
    found = [
        f"{scenario.scenario_id}:{trigger}"
        for scenario in scenarios
        for trigger in scenario.hard_triggers
    ]
    found.extend(live)
    return tuple(dict.fromkeys(found))


def _bound(items: Iterable[_Contribution], side: str) -> Decimal:
    return sum((getattr(item, side) or ZERO for item in items), ZERO)


def _dominant(contributions: list[_Contribution], *, triggered: bool = False) -> str | None:
    candidates = [
        item
        for item in contributions
        if item.scenario.affected_path and (not triggered or item.scenario.hard_triggers)
    ]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (
            bool(item.scenario.hard_triggers),
            item.central if item.central is not None else item.scenario.impact,
            item.scenario.impact,
        ),
    )
    if best.scenario.impact == ZERO and not best.scenario.hard_triggers:
        return None
    return best.scenario.affected_path[-1]


def _exit_confidence(estimate: ExitEstimate) -> ConfidenceLevel:
    if estimate.freshness is FreshnessStatus.FRESH:
        return ConfidenceLevel.HIGH
    if estimate.freshness is FreshnessStatus.STALE_USABLE:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.UNAVAILABLE


def _lowest(levels: Iterable[ConfidenceLevel]) -> ConfidenceLevel:
    return min(levels, key=CONFIDENCE_ORDER.index, default=ConfidenceLevel.HIGH)


def _lower(level: ConfidenceLevel) -> ConfidenceLevel:
    index = CONFIDENCE_ORDER.index(level)
    return CONFIDENCE_ORDER[max(1, index - 1)] if index > 0 else level


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(NUMERAIRE_PLACES)
