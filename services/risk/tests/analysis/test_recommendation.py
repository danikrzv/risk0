from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.analysis.recommendation import make_recommendation
from app.contracts.models import (
    ConfidenceLevel,
    ExitEstimate,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
    RecommendationDecision,
    ScenarioResult,
)
from app.infrastructure.freshness import DataConflict, DataQuality, Freshness


NOW = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)
ASSET = "asset:1:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ORACLE = "oracle:1:0x00000000000000000000000000000000000000c1"
STAY = RecommendationDecision.STAY
EXIT = RecommendationDecision.EXIT
UNAVAILABLE = RecommendationDecision.UNAVAILABLE


def probability(
    central: str | None = "0.02",
    low: str | None = "0.01",
    high: str | None = "0.03",
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
) -> ProbabilityEstimate:
    unavailable = central is None
    return ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE if unavailable else ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=None if unavailable or low is None else Decimal(low),
        central=None if unavailable else Decimal(central),
        high=None if unavailable or high is None else Decimal(high),
        confidence=ConfidenceLevel.UNAVAILABLE if unavailable else confidence,
        horizon_days=30,
        source="chainlink:ETH/USD",
        retrieved_at=NOW,
        freshness=FreshnessStatus.EXPIRED if unavailable else FreshnessStatus.FRESH,
        explanation="fixture",
    )


def scenario(
    impact: str = "100",
    *,
    scenario_id: str = "collateral-shock",
    triggers: tuple[str, ...] = (),
    prob: ProbabilityEstimate | None = None,
    complete: bool = True,
    confidence: ConfidenceLevel | None = None,
    shortfall: str = "0",
    path: tuple[str, ...] = ("position:1:p", ASSET),
) -> ScenarioResult:
    prob = probability() if prob is None else prob
    if confidence is None:
        confidence = (
            ConfidenceLevel.UNAVAILABLE
            if not complete or prob.confidence is ConfidenceLevel.UNAVAILABLE
            else ConfidenceLevel.HIGH
        )
    return ScenarioResult(
        scenario_id=scenario_id,
        affected_path=path,
        stressed_values={"impact_complete": complete},
        hard_triggers=triggers,
        impact=Decimal(impact),
        impact_components={
            "asset_impairment": Decimal(impact),
            "liquidity_shortfall": Decimal(shortfall),
        },
        probability=prob,
        confidence=confidence,
        explanation="fixture",
    )


def exit_estimate(
    total_cost: str = "5",
    *,
    withdrawable: bool = True,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
) -> ExitEstimate:
    return ExitEstimate(
        withdrawable=withdrawable,
        route=("WETH", "USDC@0.05%"),
        slippage=Decimal("0"),
        fees=Decimal("0"),
        gas=Decimal(total_cost),
        price_impact=Decimal("0"),
        total_cost=Decimal(total_cost),
        quote_age_seconds=3,
        quote_block=26161049,
        quoted_at=NOW,
        source="uniswap-v3:quoter-v2",
        retrieved_at=NOW,
        freshness=freshness,
    )


def quality(*missing: str, conflicts: tuple[DataConflict, ...] = ()) -> DataQuality:
    return DataQuality(
        freshness=(Freshness.FRESH,), conflicts=conflicts, critical_missing=missing
    )


# Step 1: truth table


def test_missing_critical_data_is_unavailable_even_with_a_hard_trigger():
    result = make_recommendation(
        (scenario(triggers=("liquidation",)),), exit_estimate(), quality("uniswap quote")
    )

    assert result.decision is UNAVAILABLE
    assert result.confidence is ConfidenceLevel.UNAVAILABLE
    assert "uniswap quote" in result.explanation
    assert result.triggers == ()


def test_live_oracle_trigger_exits():
    result = make_recommendation(
        (scenario("0"),),
        exit_estimate("5"),
        quality(),
        live_triggers=("live:oracle_failure",),
    )

    assert result.decision is EXIT
    assert result.triggers == ("live:oracle_failure",)


def test_oracle_failure_scenario_live_breach_exits():
    result = make_recommendation(
        (scenario("0", scenario_id="oracle-failure", triggers=("oracle_failure",), path=("p", ORACLE)),),
        exit_estimate(),
        quality(),
    )

    assert result.decision is EXIT
    assert result.triggers == ("oracle-failure:oracle_failure",)
    assert result.dominant_dependency == ORACLE


def test_liquidation_trigger_exits_even_when_loss_is_below_exit_cost():
    result = make_recommendation(
        (scenario("1", triggers=("liquidation",)),), exit_estimate("1000"), quality()
    )

    assert result.decision is EXIT
    assert "collateral-shock:liquidation" in result.triggers
    assert result.dominant_dependency == ASSET


def test_protocol_emergency_exits():
    result = make_recommendation(
        (scenario("0"),), exit_estimate(), quality(), live_triggers=("live:protocol_emergency",)
    )

    assert result.decision is EXIT


def test_lender_stressed_liquidity_below_twenty_percent_exits():
    result = make_recommendation(
        (
            scenario(
                "0",
                scenario_id="liquidity-collapse",
                triggers=("withdrawal_shortfall",),
                prob=probability(None),
                shortfall="900",
            ),
        ),
        exit_estimate("5", withdrawable=False),
        quality(),
    )

    assert result.decision is EXIT
    assert result.triggers == ("liquidity-collapse:withdrawal_shortfall",)
    assert "exit is recommended, but a full exit is currently unavailable" in result.explanation.lower()


def test_expected_loss_greater_than_exit_cost_exits():
    # 0.02 * 300 = 6 > 5
    result = make_recommendation((scenario("300"),), exit_estimate("5"), quality())

    assert result.decision is EXIT
    assert result.expected_loss == Decimal("6")


def test_equal_expected_loss_and_exit_cost_stays():
    # 0.02 * 250 = 5 == 5
    result = make_recommendation((scenario("250"),), exit_estimate("5"), quality())

    assert result.decision is STAY
    assert result.expected_loss == Decimal("5")
    assert result.exit_cost == Decimal("5")


def test_expected_loss_lower_than_exit_cost_stays():
    result = make_recommendation((scenario("100"),), exit_estimate("5"), quality())

    assert result.decision is STAY
    assert result.expected_loss == Decimal("2")
    assert result.expected_loss_low == Decimal("1")
    assert result.expected_loss_high == Decimal("3")
    assert result.confidence is ConfidenceLevel.HIGH
    assert result.dominant_dependency == ASSET
    assert "STAY" in result.explanation


# Unknown probabilities and incomplete impacts


def test_unavailable_probability_with_non_zero_impact_is_unavailable_never_zero():
    result = make_recommendation(
        (scenario("100", prob=probability(None)),), exit_estimate("5"), quality()
    )

    assert result.decision is UNAVAILABLE
    assert result.confidence is ConfidenceLevel.UNAVAILABLE
    assert "collateral-shock" in result.explanation


def test_unavailable_probability_with_zero_impact_does_not_block_the_decision():
    result = make_recommendation(
        (
            scenario("100"),
            scenario("0", scenario_id="liquidity-collapse", prob=probability(None)),
        ),
        exit_estimate("5"),
        quality(),
    )

    assert result.decision is STAY
    assert result.confidence is ConfidenceLevel.HIGH


def test_incomplete_impact_is_unavailable_even_when_the_known_loss_is_small():
    # The impact on the modelled exposure is known, but an unmodelled share is not.
    result = make_recommendation(
        (scenario("1", complete=False),), exit_estimate("5"), quality()
    )

    assert result.decision is UNAVAILABLE
    assert "complete impact for collateral-shock" in result.explanation


def test_unavailable_scenario_without_completeness_flag_and_known_probability_is_unavailable():
    legacy = scenario("1", confidence=ConfidenceLevel.UNAVAILABLE).model_copy(
        update={"stressed_values": {}}
    )

    result = make_recommendation((legacy,), exit_estimate("5"), quality())

    assert result.decision is UNAVAILABLE


def test_expired_exit_estimate_is_unavailable():
    result = make_recommendation(
        (scenario("100"),), exit_estimate("5", freshness=FreshnessStatus.EXPIRED), quality()
    )

    assert result.decision is UNAVAILABLE


def test_missing_critical_data_never_becomes_stay():
    for missing in ("morpho snapshot", "vault allocations", "live oracle", "exit estimate"):
        result = make_recommendation((scenario("0"),), exit_estimate("5"), quality(missing))
        assert result.decision is UNAVAILABLE


# Step 2: expected-loss arithmetic and conflicts


def test_expected_loss_is_sum_of_central_probability_times_impact():
    result = make_recommendation(
        (
            scenario("100", prob=probability("0.02", "0.01", "0.03")),
            scenario(
                "50",
                scenario_id="protocol-impairment",
                prob=probability("0.004", "0.002", "0.008"),
            ),
        ),
        exit_estimate("100"),
        quality(),
    )

    assert result.expected_loss == Decimal("2.2")  # 0.02*100 + 0.004*50
    assert result.expected_loss_low == Decimal("1.1")  # 0.01*100 + 0.002*50
    assert result.expected_loss_high == Decimal("3.4")  # 0.03*100 + 0.008*50


def test_expected_loss_does_not_double_count_a_scenario_selected_twice():
    result = make_recommendation(
        (scenario("100"), scenario("200")),
        exit_estimate("100"),
        quality(),
    )

    # Same event at two severities counts once, at its larger expected loss.
    assert result.expected_loss == Decimal("4")


def test_expected_loss_excludes_the_liquidity_shortfall():
    result = make_recommendation(
        (scenario("0", scenario_id="liquidity-collapse", shortfall="900"),),
        exit_estimate("5"),
        quality(),
    )

    assert result.expected_loss == Decimal("0")
    assert result.decision is STAY


def test_conflicting_sources_lower_confidence_and_are_explained():
    conflict = DataConflict(
        field="probability:collateral-shock",
        values=("0.02", "0.05"),
        sources=("chainlink:ETH/USD", "fixture:secondary"),
    )

    result = make_recommendation(
        (scenario("100"),), exit_estimate("5"), quality(conflicts=(conflict,))
    )

    assert result.decision is STAY
    assert result.confidence is ConfidenceLevel.MEDIUM
    assert "conservative" in result.explanation


def test_confidence_is_the_lowest_relevant_level():
    result = make_recommendation(
        (
            scenario("100", prob=probability(confidence=ConfidenceLevel.LOW), confidence=ConfidenceLevel.LOW),
            scenario("10", scenario_id="protocol-impairment"),
        ),
        exit_estimate("100", freshness=FreshnessStatus.STALE_USABLE),
        quality(),
    )

    assert result.confidence is ConfidenceLevel.LOW


@pytest.mark.parametrize("withdrawable", [True, False])
def test_exit_explanation_mentions_unavailable_full_exit_only_when_not_withdrawable(withdrawable):
    result = make_recommendation(
        (scenario("300"),), exit_estimate("5", withdrawable=withdrawable), quality()
    )

    assert result.decision is EXIT
    mentioned = "full exit is currently unavailable" in result.explanation
    assert mentioned is (not withdrawable)


# Live triggers name their node and carry their data confidence


def test_live_oracle_trigger_names_the_oracle_and_is_the_dominant_dependency():
    from app.analysis.recommendation import LiveTrigger

    result = make_recommendation(
        (scenario("0"),),
        exit_estimate(),
        quality(),
        live_triggers=(LiveTrigger("live:oracle_failure", ORACLE, ConfidenceLevel.MEDIUM),),
    )

    assert result.decision is EXIT
    assert result.triggers == (f"live:oracle_failure:{ORACLE}",)
    assert result.dominant_dependency == ORACLE
    assert result.confidence is ConfidenceLevel.MEDIUM  # lowest of live data and exit (HIGH)
    assert ORACLE in result.explanation


def test_scenario_trigger_stays_dominant_over_a_live_trigger():
    from app.analysis.recommendation import LiveTrigger

    result = make_recommendation(
        (scenario("100", triggers=("liquidation",)),),
        exit_estimate(),
        quality(),
        live_triggers=(LiveTrigger("live:oracle_failure", ORACLE, ConfidenceLevel.HIGH),),
    )

    assert result.triggers == ("collateral-shock:liquidation", f"live:oracle_failure:{ORACLE}")
    assert result.dominant_dependency == ASSET


# Break-even probability


def only_point(result):
    assert result.break_even is not None
    assert len(result.break_even.points) == 1
    return result.break_even.points[0]


def test_break_even_is_exit_cost_over_impact_with_the_current_estimate():
    result = make_recommendation((scenario("100"),), exit_estimate("5"), quality())

    point = only_point(result)
    assert result.decision is STAY
    assert result.break_even.probability_dependent is True
    assert result.break_even.dominant_scenario_id == "collateral-shock"
    assert result.break_even.exit_cost == Decimal("5")
    assert point.break_even_probability == Decimal("0.05")
    assert point.decision_flip_probability == Decimal("0.05")
    assert point.other_expected_loss == Decimal(0)
    assert point.current_probability == Decimal("0.02")
    assert (point.current_probability_low, point.current_probability_high) == (
        Decimal("0.01"),
        Decimal("0.03"),
    )
    assert point.dependency == ASSET
    assert point.reasons == ()
    assert "exceeds 5% (current estimate 2%)" in result.break_even.explanation


def test_estimate_above_break_even_exits_and_the_flip_accounts_for_other_scenarios():
    result = make_recommendation(
        (
            scenario("100", prob=probability("0.08", "0.04", "0.1")),
            scenario("50", scenario_id="protocol-impairment", path=("p", "protocol:morpho")),
        ),
        exit_estimate("6"),
        quality(),
    )

    assert result.decision is EXIT  # 8 + 1 > 6
    shock, protocol = result.break_even.points
    assert result.break_even.dominant_scenario_id == "collateral-shock"
    assert shock.break_even_probability == Decimal("0.06")
    # (6 - 50 * 0.02) / 100
    assert shock.other_expected_loss == Decimal(1)
    assert shock.decision_flip_probability == Decimal("0.05")
    assert protocol.break_even_probability == Decimal("0.12")
    # (6 - 8) / 50 is negative: capped at 0, EXIT already from the other scenario.
    assert protocol.decision_flip_probability == Decimal(0)
    assert any("already exceeds" in reason for reason in protocol.reasons)


def test_exit_cost_above_the_impact_caps_break_even_at_one():
    result = make_recommendation((scenario("4"),), exit_estimate("5"), quality())

    point = only_point(result)
    assert point.break_even_probability == Decimal(1)
    assert point.decision_flip_probability == Decimal(1)
    assert any("no probability" in reason for reason in point.reasons)
    assert "No 30-day probability of collateral-shock makes EXIT cheaper" in result.break_even.explanation


def test_zero_impact_scenarios_have_no_break_even_point():
    result = make_recommendation((scenario("0"),), exit_estimate("5"), quality())

    assert result.break_even.points == ()
    assert result.break_even.dominant_scenario_id is None
    assert "no break-even" in result.break_even.explanation


def test_incomplete_impact_gives_a_null_break_even_with_a_reason():
    result = make_recommendation((scenario("100", complete=False),), exit_estimate("5"), quality())

    point = only_point(result)
    assert result.decision is UNAVAILABLE
    assert point.break_even_probability is None
    assert point.decision_flip_probability is None
    assert "incomplete" in point.reasons[0]


def test_unknown_exit_cost_gives_a_null_break_even_never_zero():
    result = make_recommendation(
        (scenario("100"),), exit_estimate("0", freshness=FreshnessStatus.EXPIRED), quality()
    )

    point = only_point(result)
    assert result.break_even.exit_cost is None
    assert point.break_even_probability is None
    assert "exit cost is unknown" in point.reasons[0]


def test_unknown_probability_keeps_the_break_even_but_not_the_flip_of_others():
    result = make_recommendation(
        (
            scenario("100", prob=probability(None)),
            scenario("50", scenario_id="protocol-impairment", path=("p", "protocol:morpho")),
        ),
        exit_estimate("5"),
        quality(),
    )

    assert result.decision is UNAVAILABLE
    unknown, protocol = result.break_even.points
    assert unknown.break_even_probability == Decimal("0.05")
    assert unknown.decision_flip_probability == Decimal("0.04")  # (5 - 1) / 100
    assert unknown.current_probability is None
    assert any("no current 30-day probability" in reason for reason in unknown.reasons)
    assert protocol.break_even_probability == Decimal("0.1")
    assert protocol.decision_flip_probability is None
    assert any("collateral-shock is unknown" in reason for reason in protocol.reasons)


def test_hard_trigger_decision_does_not_depend_on_probability():
    result = make_recommendation(
        (scenario("100", triggers=("liquidation",)),), exit_estimate("5"), quality()
    )

    assert result.decision is EXIT
    assert result.break_even.probability_dependent is False
    assert "does not depend on any probability" in result.break_even.explanation
    # The threshold is still reported for context.
    assert only_point(result).break_even_probability == Decimal("0.05")


def test_break_even_serializes_to_the_response_schema():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "packages/contracts/schemas/analysis-response.schema.json"
        ).read_text()
    )
    validator = Draft202012Validator({"$ref": "#/$defs/Recommendation", "$defs": schema["$defs"]})
    for exit_cost in ("5", "500"):
        result = make_recommendation((scenario("100"),), exit_estimate(exit_cost), quality())
        validator.validate(json.loads(result.model_dump_json()))
