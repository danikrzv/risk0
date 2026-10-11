"""Hybrid STAY / EXIT / UNAVAILABLE recommendation (spec § 13).

    expected_staying_loss_30d = sum(event_probability_30d * event_impact)
    forgone_yield_30d         = amount * apy * 30 / 365        (lenders)
    cost_of_exiting           = exit_cost_now + forgone_yield_30d

    if critical_data_missing:                  UNAVAILABLE
    elif any_hard_safety_trigger:              EXIT
    elif expected_loss_incomplete:             UNAVAILABLE
    elif expected_loss > cost_of_exiting:      EXIT
    else:                                      STAY   (equal values stay)

Carry: staying earns the position's yield and exiting to cash gives it up, so
the fair comparison for a yield manager is the expected loss against the exit
cost plus the yield forgone over the same 30-day horizon. Without it, a lender
whose exit costs only gas would be told to EXIT at any non-zero risk.
- Lender: `forgone_yield = amount * apy * horizon_days / 365` in the position
  numeraire, with the vault's net APY for a vault position and the market's
  supply APY for a market position (net of fees when Morpho reports it).
  Exited capital is assumed to be held idle (no alternative yield); crediting a
  redeployment yield is future work.
- Unknown APY: the forgone yield is counted as zero (which biases toward EXIT,
  the conservative side), a warning is added, and the confidence of a decision
  made by the comparison is lowered one level.
- Borrower: carry (borrow cost versus collateral yield) is out of scope; the
  forgone yield is zero and the explanation says so.
- Hard triggers and UNAVAILABLE rules do not depend on carry.
- Without a carry estimate (older callers) the forgone yield is zero and the
  carry fields of the recommendation are null.

Critical data is missing when `data_quality.critical_missing` is non-empty, when
the exit estimate is EXPIRED (an expired quote cannot support a decision), when
any scenario reports `stressed_values["impact_complete"] is False` (missing,
expired, or unmodelled inputs), or when a scenario's impact is UNAVAILABLE with
no completeness flag at all. Missing data can never become STAY.

Hard triggers come from the scenarios (borrower liquidation, live oracle breach,
lender withdrawal shortfall) and from `live_triggers` the orchestrator observed
in current data regardless of the selected scenarios (live oracle breach,
protocol emergency). A hard trigger is a deterministic consequence of current
data or of a selected stress, so it does not need a probability. A live trigger
(`LiveTrigger`) names the graph node that breached (reported as
`<kind>:<node_id>`, e.g. `live:oracle_failure:oracle:1:0x...`), which becomes the
dominant dependency when no scenario trigger is dominant, and carries the
confidence of the live data it rests on; the EXIT confidence is the lowest of
the triggering scenarios' data confidence (a trigger does not use the
scenario's probability, so the probability's confidence does not lower it), the
live data, and the exit estimate. The dominant dependency is the nearest
shocked node of the dominant scenario (`primary_dependency`), so a protocol
event names Morpho rather than an issuer reached through it.

The expected loss is incomplete when a scenario with a non-zero impact has no
central probability: an unknown probability is never treated as zero.

No double counting: `impact` already excludes the liquidity shortfall (it is
reported but is not a realized loss), and a scenario id selected more than once
(for example at two severities) contributes once, with its largest expected
loss, because the selections describe the same event. Each selection's expected
loss pairs its own impact with its own probability (the orchestrator estimates a
probability per (scenario id, severity)), so a mild severity's higher
probability is never applied to a severe impact.

Break-even (`Recommendation.break_even`): for every deduplicated scenario with a
non-zero impact, the 30-day probability at which staying and exiting cost the
same, with `cost_of_exiting = exit_cost + forgone_yield` as the cost of exiting.
`break_even_probability = cost_of_exiting / impact` is that scenario on its own;
`decision_flip_probability = (cost_of_exiting - other_expected_loss) / impact`
holds the other scenarios at their central estimates, so it is where this rule
flips.
Both are capped to [0, 1]: 1 means no probability makes EXIT cheaper, 0 means
EXIT is already cheaper at zero probability. A value is null, with a reason,
when the impact is incomplete or negative, the exit cost is unknown (expired or
missing exit estimate), or (for the flip) another scenario's probability is
unknown; nothing is fabricated. When a hard trigger fires the decision does not
depend on probability, and `probability_dependent` is false.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.contracts.models import (
    BreakEven,
    BreakEvenPoint,
    Carry,
    CarryStatus,
    ConfidenceLevel,
    ExitEstimate,
    FreshnessStatus,
    PositionMode,
    Recommendation,
    RecommendationDecision,
    ScenarioResult,
)
from app.domain.exit import NUMERAIRE_PLACES
from app.evidence.scoring import DAYS_PER_YEAR, SUPPORTED_HORIZON_DAYS
from app.infrastructure.freshness import DataQuality


ZERO = Decimal(0)
ONE = Decimal(1)
IMPACT_COMPLETE_KEY = "impact_complete"
DATA_CONFIDENCE_KEY = "data_confidence"
PRIMARY_DEPENDENCY_KEY = "primary_dependency"
CONFIDENCE_ORDER = (
    ConfidenceLevel.UNAVAILABLE,
    ConfidenceLevel.LOW,
    ConfidenceLevel.MEDIUM,
    ConfidenceLevel.HIGH,
)
LIVE_ORACLE_FAILURE = "live:oracle_failure"
PROTOCOL_EMERGENCY = "live:protocol_emergency"
HORIZON_DAYS = SUPPORTED_HORIZON_DAYS
IDLE_CAPITAL_NOTE = (
    "Assumes exited capital is held idle (no alternative yield), so exiting gives up "
    "the whole position yield over the horizon."
)
UNKNOWN_APY_NOTE = (
    "The position APY is unknown: the forgone yield is counted as zero, which biases "
    "toward EXIT, and the confidence of the comparison was lowered."
)
BORROWER_CARRY_NOTE = (
    "Borrower carry (borrow cost versus collateral yield) is not modelled: the "
    "forgone yield is zero."
)


@dataclass(frozen=True, slots=True)
class CarryEstimate:
    """The position yield over the horizon (`carry`) and what exiting gives up."""

    carry: Carry
    forgone_yield: Decimal


def estimate_carry(
    mode: PositionMode,
    amount: Decimal,
    apy: Decimal | None,
    *,
    apy_source: str | None = None,
    horizon_days: int = HORIZON_DAYS,
) -> CarryEstimate:
    """Forgone yield of exiting: `amount * apy * horizon_days / 365` for a lender.

    `amount` is in the position numeraire, so the result is too. An unknown APY
    gives zero with an UNKNOWN status (never a guessed yield); a borrower gives
    zero with a NOT_APPLICABLE status.
    """
    if mode is PositionMode.BORROWER:
        return CarryEstimate(
            carry=Carry(
                status=CarryStatus.NOT_APPLICABLE,
                apy=None,
                apy_source=None,
                horizon_days=horizon_days,
                notes=(BORROWER_CARRY_NOTE,),
            ),
            forgone_yield=_quantize(ZERO),
        )
    if apy is None or not apy.is_finite() or apy < ZERO:
        return CarryEstimate(
            carry=Carry(
                status=CarryStatus.UNKNOWN,
                apy=None,
                apy_source=apy_source,
                horizon_days=horizon_days,
                notes=(UNKNOWN_APY_NOTE, IDLE_CAPITAL_NOTE),
            ),
            forgone_yield=_quantize(ZERO),
        )
    forgone = amount * apy * Decimal(horizon_days) / Decimal(DAYS_PER_YEAR)
    return CarryEstimate(
        carry=Carry(
            status=CarryStatus.KNOWN,
            apy=apy,
            apy_source=apy_source,
            horizon_days=horizon_days,
            notes=(IDLE_CAPITAL_NOTE,),
        ),
        forgone_yield=_quantize(max(ZERO, forgone)),
    )


@dataclass(frozen=True, slots=True)
class LiveTrigger:
    """A hard trigger observed in current data, independent of the selected scenarios."""

    kind: str
    node_id: str | None = None
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.node_id}" if self.node_id else self.kind


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
    live_triggers: Iterable[str | LiveTrigger] = (),
    numeraire: str = "",
    carry: CarryEstimate | None = None,
    confidence_caps: Iterable[tuple[ConfidenceLevel, str]] = (),
) -> Recommendation:
    """`carry` is the position yield; without it the forgone yield is zero (older callers).

    `confidence_caps` are (highest allowed confidence, reason) pairs for data that
    is usable but only partly judged (for example a live oracle whose
    third-party feed staleness is unknown). Each cap bounds a STAY or EXIT
    confidence and adds its reason to the explanation.
    """
    unit = f" {numeraire}" if numeraire else ""
    live = tuple(
        item if isinstance(item, LiveTrigger) else LiveTrigger(item) for item in live_triggers
    )
    exit_cost = exit_estimate.total_cost
    forgone = carry.forgone_yield if carry is not None else ZERO
    cost_of_exiting = exit_cost + forgone
    carry_fields = {
        "forgone_yield": None if carry is None else forgone,
        "cost_of_exiting": None if carry is None else cost_of_exiting,
        "carry": None if carry is None else carry.carry,
    }
    contributions = _deduplicated(scenarios)
    break_even = _break_even(
        contributions,
        exit_estimate,
        triggered=bool(_triggers(scenarios, live)),
        forgone_yield=None if carry is None else forgone,
    )
    known =[item for item in contributions if item.central is not None]
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
            break_even=break_even,
            **carry_fields,
        )

    triggers = _triggers(scenarios, live)
    unknown_probability = tuple(
        item.scenario.scenario_id
        for item in contributions
        if item.central is None and item.scenario.impact != ZERO
    )
    loss_low = None if unknown_probability else _quantize(_bound(known, "low"))
    loss_high = None if unknown_probability else _quantize(_bound(known, "high"))
    dominant = _dominant(contributions, triggered=bool(triggers))
    if dominant is None and triggers:
        dominant = next((item.node_id for item in live if item.node_id), None)
    conflicts = data_quality.conflicts
    comparison = (
        f"expected 30-day staying loss {expected_loss}{unit}"
        + (f" (range {loss_low} to {loss_high}{unit})" if loss_low is not None else "")
        + f" versus exit cost now {exit_cost}{unit}"
        + _carry_comparison(carry, cost_of_exiting, unit)
    )
    cost_label = "exit cost" if carry is None else "cost of exiting"

    if triggers:
        decision = RecommendationDecision.EXIT
        confidence = _lowest(
            [
                *(_trigger_confidence(item.scenario) for item in contributions if item.scenario.hard_triggers),
                *(item.confidence for item in live),
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
            break_even=break_even,
            **carry_fields,
        )
    else:
        relevant = [
            item.scenario.confidence
            for item in contributions
            if item.scenario.impact != ZERO
        ]
        confidence = _lowest([*relevant, _exit_confidence(exit_estimate)])
        if carry is not None and carry.carry.status is CarryStatus.UNKNOWN:
            confidence = _lower(confidence)
        if expected_loss > cost_of_exiting:
            decision = RecommendationDecision.EXIT
            parts = [f"EXIT: the {comparison}; the expected loss is higher."]
        else:
            decision = RecommendationDecision.STAY
            parts = [
                f"STAY: the {comparison}; the expected loss does not exceed the {cost_label}."
            ]
        if carry is not None:
            parts.extend(carry.carry.notes)

    if dominant is not None:
        parts.append(f"Dominant dependency: {dominant}.")
    if conflicts:
        confidence = _lower(confidence)
        fields = ", ".join(dict.fromkeys(conflict.field for conflict in conflicts))
        parts.append(
            f"Sources conflict on {fields}; the conservative value was used and "
            "confidence was lowered."
        )
    for cap, reason in confidence_caps:
        if CONFIDENCE_ORDER.index(cap) < CONFIDENCE_ORDER.index(confidence):
            confidence = cap
        parts.append(reason)
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
        break_even=break_even,
        **carry_fields,
    )


def _carry_comparison(carry: CarryEstimate | None, cost_of_exiting: Decimal, unit: str) -> str:
    if carry is None:
        return ""
    status = carry.carry.status
    if status is CarryStatus.KNOWN and carry.carry.apy is not None:
        basis = f"APY {_percent(carry.carry.apy)}"
    elif status is CarryStatus.UNKNOWN:
        basis = "APY unknown, counted as zero"
    else:
        basis = "borrower carry not modelled"
    return (
        f" plus forgone {carry.carry.horizon_days}-day yield {carry.forgone_yield}{unit} "
        f"({basis}), a cost of exiting of {cost_of_exiting}{unit}"
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


def _triggers(
    scenarios: Iterable[ScenarioResult], live: Iterable[LiveTrigger]
) -> tuple[str, ...]:
    found = [
        f"{scenario.scenario_id}:{trigger}"
        for scenario in scenarios
        for trigger in scenario.hard_triggers
    ]
    found.extend(item.label for item in live)
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
    primary = best.scenario.stressed_values.get(PRIMARY_DEPENDENCY_KEY)
    return primary if isinstance(primary, str) else best.scenario.affected_path[-1]


def _trigger_confidence(scenario: ScenarioResult) -> ConfidenceLevel:
    """A hard trigger follows from the data, so its probability's confidence does not apply."""
    value = scenario.stressed_values.get(DATA_CONFIDENCE_KEY)
    try:
        return ConfidenceLevel(value) if isinstance(value, str) else scenario.confidence
    except ValueError:
        return scenario.confidence


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


def _break_even(
    contributions: list[_Contribution],
    exit_estimate: ExitEstimate,
    *,
    triggered: bool,
    forgone_yield: Decimal | None = None,
) -> BreakEven:
    # The unavailable exit estimate is EXPIRED with a zero total: unknown, not free.
    exit_cost = (
        None if exit_estimate.freshness is FreshnessStatus.EXPIRED else exit_estimate.total_cost
    )
    # The cost of exiting: the exit cost now plus the yield exiting gives up.
    cost = None if exit_cost is None else exit_cost + (forgone_yield or ZERO)
    candidates = [item for item in contributions if item.scenario.impact != ZERO]
    points = [_break_even_point(item, contributions, cost) for item in candidates]
    dominant = max(
        range(len(candidates)),
        key=lambda index: (
            candidates[index].central
            if candidates[index].central is not None
            else candidates[index].scenario.impact,
            candidates[index].scenario.impact,
        ),
        default=None,
    )
    if triggered:
        explanation = (
            "A hard safety trigger fired: it overrides the expected-loss comparison, so the "
            "stay/exit decision does not depend on any probability."
        )
    elif dominant is None:
        explanation = "No selected scenario has a non-zero impact, so there is no break-even probability."
    else:
        explanation = _point_sentence(points[dominant])
    return BreakEven(
        probability_dependent=not triggered,
        dominant_scenario_id=None if dominant is None else points[dominant].scenario_id,
        exit_cost=exit_cost,
        points=tuple(points),
        explanation=explanation,
        forgone_yield=forgone_yield,
        cost_of_exiting=None if forgone_yield is None else cost,
    )


def _break_even_point(
    item: _Contribution, contributions: list[_Contribution], cost: Decimal | None
) -> BreakEvenPoint:
    scenario = item.scenario
    impact = scenario.impact
    probability = scenario.probability
    others = [other for other in contributions if other is not item]
    other_unknown = [
        other.scenario.scenario_id
        for other in others
        if other.central is None and other.scenario.impact != ZERO
    ]
    other_loss = (
        None
        if other_unknown
        else _quantize(sum((other.central or ZERO for other in others), ZERO))
    )
    reasons: list[str] = []
    break_even = flip = None
    if _impact_incomplete(scenario):
        reasons.append("the scenario impact is incomplete, so no break-even is computed")
    elif impact < ZERO:
        reasons.append("the scenario impact is negative, so it has no break-even probability")
    elif cost is None:
        reasons.append("the exit cost is unknown (no current exit estimate)")
    else:
        break_even = _probability(cost / impact)
        if cost >= impact:
            reasons.append(
                "the cost of exiting is at least the full impact: no probability of this scenario "
                "alone makes EXIT cheaper"
            )
        if other_loss is None:
            reasons.append(
                "no decision flip point: the probability of "
                f"{', '.join(other_unknown)} is unknown"
            )
        else:
            flip = _probability((cost - other_loss) / impact)
            if cost < other_loss:
                reasons.append("the other scenarios' expected loss already exceeds the cost of exiting")
    if probability.central is None:
        reasons.append("no current 30-day probability estimate is available")
    return BreakEvenPoint(
        scenario_id=scenario.scenario_id,
        dependency=scenario.affected_path[-1] if scenario.affected_path else None,
        impact=impact,
        other_expected_loss=other_loss,
        break_even_probability=break_even,
        decision_flip_probability=flip,
        current_probability=_clamped(probability.central),
        current_probability_low=_clamped(probability.low),
        current_probability_high=_clamped(probability.high),
        reasons=tuple(reasons),
    )


def _point_sentence(point: BreakEvenPoint) -> str:
    threshold = point.decision_flip_probability
    if threshold is None:
        threshold = point.break_even_probability
    if threshold is None:
        return f"No break-even probability for {point.scenario_id}: {'; '.join(point.reasons)}."
    estimate = (
        "no current estimate"
        if point.current_probability is None
        else f"current estimate {_percent(point.current_probability)}"
    )
    if threshold >= ONE:
        return (
            f"No 30-day probability of {point.scenario_id} makes EXIT cheaper than staying "
            f"({estimate})."
        )
    if threshold <= ZERO and point.decision_flip_probability is not None:
        return (
            "The other scenarios' expected loss alone reaches the cost of exiting, so EXIT is "
            f"optimal at any non-zero probability of {point.scenario_id} ({estimate})."
        )
    return (
        f"EXIT becomes optimal if the 30-day probability of {point.scenario_id} exceeds "
        f"{_percent(threshold)} ({estimate})."
    )


def _percent(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.0001')).normalize():f}%"


def _probability(value: Decimal) -> Decimal:
    return _quantize(min(ONE, max(ZERO, value)))


def _clamped(value: Decimal | None) -> Decimal | None:
    return None if value is None else min(ONE, max(ZERO, value))
