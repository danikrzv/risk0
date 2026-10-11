import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analysis.book import POCKET_FAILED, run_book
from app.analysis.service import AnalysisValidationError
from app.contracts.models import (
    AnalysisRequest,
    AnalysisResponse,
    ConfidenceLevel,
    ExitEstimate,
    FreshnessStatus,
    ProbabilityEstimate,
    ProbabilityMethod,
    Recommendation,
    RecommendationDecision,
    RiskGraph,
    RiskNode,
    RiskNodeType,
    ScenarioResult,
    BookRequest,
)


NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
STAY = RecommendationDecision.STAY
EXIT = RecommendationDecision.EXIT
UNAVAILABLE = RecommendationDecision.UNAVAILABLE
SCENARIOS = [{"scenario_id": "collateral-shock", "severity": "0.15"}]


def probability(central: str | None) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        method=ProbabilityMethod.UNAVAILABLE if central is None else ProbabilityMethod.EMPIRICAL_ROLLING_WINDOW,
        low=None,
        central=None if central is None else Decimal(central),
        high=None,
        confidence=ConfidenceLevel.UNAVAILABLE if central is None else ConfidenceLevel.HIGH,
        horizon_days=30,
        source="fixture",
        retrieved_at=NOW,
        freshness=FreshnessStatus.FRESH,
        explanation="fixture",
    )


def response(
    *,
    decision=STAY,
    expected_loss="1",
    exit_cost="5",
    central: str | None = "0.01",
    triggers=(),
    collateral_value=None,
    debt_value=None,
    exit_freshness=FreshnessStatus.FRESH,
) -> AnalysisResponse:
    position = RiskNode(
        node_id="position:1:p",
        node_type=RiskNodeType.POSITION,
        label="position",
        metrics={"collateral_value": collateral_value, "debt_value": debt_value},
        provenance="user input",
        freshness=FreshnessStatus.FRESH,
        observed_at=NOW,
    )
    return AnalysisResponse(
        graph=RiskGraph(nodes=(position,), edges=(), generated_at=NOW),
        scenario_results=(
            ScenarioResult(
                scenario_id="collateral-shock",
                affected_path=("position:1:p", "asset:1:weth"),
                stressed_values={"impact_complete": True},
                hard_triggers=(),
                impact=Decimal(100),
                impact_components={},
                probability=probability(central),
                confidence=ConfidenceLevel.HIGH,
                explanation="fixture",
            ),
        ),
        exit_estimate=ExitEstimate(
            withdrawable=True,
            route=(),
            slippage=Decimal(exit_cost),
            fees=Decimal(0),
            gas=Decimal(0),
            price_impact=Decimal(0),
            total_cost=Decimal(exit_cost),
            quote_age_seconds=1,
            quoted_at=NOW,
            source="fixture",
            retrieved_at=NOW,
            freshness=exit_freshness,
        ),
        recommendation=Recommendation(
            decision=decision,
            expected_loss=Decimal(expected_loss),
            exit_cost=Decimal(exit_cost),
            triggers=tuple(triggers),
            dominant_dependency="asset:1:weth",
            confidence=ConfidenceLevel.UNAVAILABLE if decision is UNAVAILABLE else ConfidenceLevel.HIGH,
            explanation=f"{decision.value}: fixture",
        ),
        analyzed_at=NOW,
    )


class FakeService:
    def __init__(self, outcomes: dict[str, object], delay: float = 0.0) -> None:
        self.outcomes = outcomes
        self.delay = delay
        self.requests: list[AnalysisRequest] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        with self.lock:
            self.requests.append(request)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            outcome = self.outcomes[request.position.entity_id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            with self.lock:
                self.active -= 1


def lender(entity_id: str, *, amount="1000", numeraire="USDC") -> dict:
    return {"mode": "lender", "entity_id": entity_id, "amount": amount, "numeraire": numeraire}


def book(pockets, **extra) -> BookRequest:
    return BookRequest.model_validate({"pockets": pockets, "scenarios": SCENARIOS, **extra})


def ten_pockets() -> BookRequest:
    return book([{"position": lender(f"pocket-{index}"), "weight": "0.1"} for index in range(10)])


def brief_outcomes() -> dict[str, object]:
    """Nine quiet pockets and one affected pocket that costs 11% to unwind."""
    outcomes: dict[str, object] = {f"pocket-{index}": response() for index in range(9)}
    outcomes["pocket-9"] = response(decision=EXIT, expected_loss="200", exit_cost="110", central="0.4")
    return outcomes


def test_brief_book_of_ten_pockets_reports_fractions_of_pocket_and_book():
    result = run_book(ten_pockets(), FakeService(brief_outcomes()), clock=lambda: NOW)

    affected = result.pockets[0]
    assert affected.entity_id == "pocket-9" and affected.rank == 1
    assert affected.decision is EXIT
    assert affected.pocket_value == Decimal(1000)
    assert affected.exit_cost.fraction_of_pocket == Decimal("0.11")
    assert affected.exit_cost.fraction_of_book == Decimal("0.011")
    assert affected.expected_loss.fraction_of_pocket == Decimal("0.2")
    assert affected.exit_advantage.amount == Decimal(90)
    assert affected.break_even is None  # the fake recommendation carries none
    assert [pocket.rank for pocket in result.pockets] == list(range(1, 11))

    totals = result.totals
    assert totals.numeraire == "USDC"
    assert totals.expected_loss_staying.amount == Decimal(209)
    assert totals.expected_loss_staying.fraction_of_book == Decimal("0.0209")
    assert totals.exit_cost_exit_pockets.fraction_of_book == Decimal("0.011")
    assert totals.expected_loss_stay_pockets.fraction_of_book == Decimal("0.0009")
    assert totals.recommended_cost.amount == Decimal(119)
    assert totals.net_benefit.amount == Decimal(90)
    assert totals.net_benefit.fraction_of_book == Decimal("0.009")
    assert (totals.stay_count, totals.exit_count, totals.unavailable_count) == (9, 1, 0)
    assert totals.allocated_weight == Decimal(1)
    assert totals.exit_weight == Decimal("0.1")
    assert totals.complete is True
    assert totals.warnings == ()
    assert result.generated_at == NOW


def test_book_value_turns_book_fractions_into_book_amounts_across_numeraires():
    request = book(
        [
            {"position": lender("weth", amount="10", numeraire="WETH"), "weight": "0.5"},
            {"position": lender("usdc", amount="1000"), "weight": "0.5"},
        ],
        book_value="1000000",
        numeraire="USD",
    )
    service = FakeService(
        {
            "weth": response(expected_loss="0.1", exit_cost="0.01"),
            "usdc": response(expected_loss="2", exit_cost="5"),
        }
    )

    totals = run_book(request, service).totals

    # 0.01 * 0.5 + 0.002 * 0.5 of the book
    assert totals.expected_loss_staying.fraction_of_book == Decimal("0.006")
    assert totals.expected_loss_staying.amount == Decimal(6000)
    assert totals.numeraire == "USD"
    assert totals.complete is True


def test_mixed_numeraires_without_a_book_value_report_fractions_only():
    request = book(
        [
            {"position": lender("weth", amount="10", numeraire="WETH"), "weight": "0.3"},
            {"position": lender("usdc"), "weight": "0.3"},
        ]
    )
    service = FakeService({"weth": response(expected_loss="0.1"), "usdc": response()})

    totals = run_book(request, service).totals

    assert totals.expected_loss_staying.amount is None
    assert totals.expected_loss_staying.fraction_of_book == Decimal("0.0033")
    assert totals.numeraire is None
    assert totals.allocated_weight == Decimal("0.6")
    assert totals.complete is True
    assert any("different numeraires" in warning for warning in totals.warnings)


def test_a_failing_pocket_is_unavailable_and_never_fails_the_book(caplog):
    outcomes = brief_outcomes()
    outcomes["pocket-3"] = RuntimeError("boom at https://rpc.example/secret-key")
    outcomes["pocket-4"] = AnalysisValidationError("Unknown entity_id: it is not in the curated catalog.")

    result = run_book(ten_pockets(), FakeService(outcomes))

    failed = {pocket.entity_id: pocket for pocket in result.pockets if pocket.decision is UNAVAILABLE}
    assert set(failed) == {"pocket-3", "pocket-4"}
    assert failed["pocket-3"].unavailable_reasons == (POCKET_FAILED,)
    assert "Unknown entity_id" in failed["pocket-4"].unavailable_reasons[0]
    assert failed["pocket-3"].confidence is ConfidenceLevel.UNAVAILABLE
    assert failed["pocket-3"].expected_loss is None and failed["pocket-3"].exit_cost is None
    # UNAVAILABLE pockets rank last.
    assert [pocket.entity_id for pocket in result.pockets[-2:]] == ["pocket-3", "pocket-4"]
    assert "secret-key" not in result.model_dump_json()
    assert "secret-key" not in caplog.text
    totals = result.totals
    assert totals.unavailable_count == 2
    assert totals.unavailable_weight == Decimal("0.2")
    assert totals.complete is False
    assert "2 pockets are UNAVAILABLE" in totals.warnings[0]
    # Totals cover the decided pockets only: 7 quiet pockets plus the affected one.
    assert totals.expected_loss_staying.amount == Decimal(207)


def test_an_unavailable_analysis_keeps_its_exit_cost_but_never_an_expected_loss():
    outcomes = brief_outcomes()
    outcomes["pocket-0"] = response(decision=UNAVAILABLE, expected_loss="0.5")
    outcomes["pocket-1"] = response(
        decision=UNAVAILABLE, exit_cost="0", exit_freshness=FreshnessStatus.EXPIRED
    )

    pockets = {pocket.entity_id: pocket for pocket in run_book(ten_pockets(), FakeService(outcomes)).pockets}

    assert pockets["pocket-0"].expected_loss is None
    assert pockets["pocket-0"].exit_cost.amount == Decimal(5)
    assert pockets["pocket-0"].unavailable_reasons[0] == "UNAVAILABLE: fixture"
    assert pockets["pocket-1"].exit_cost is None  # unknown, not zero


def test_hard_trigger_exit_with_an_unknown_probability_has_no_expected_loss():
    outcomes = brief_outcomes()
    outcomes["pocket-9"] = response(
        decision=EXIT, expected_loss="0", exit_cost="110", central=None, triggers=("collateral-shock:liquidation",)
    )

    result = run_book(ten_pockets(), FakeService(outcomes))

    affected = next(pocket for pocket in result.pockets if pocket.entity_id == "pocket-9")
    assert affected.decision is EXIT
    assert affected.expected_loss is None
    assert affected.exit_cost.fraction_of_pocket == Decimal("0.11")
    assert any("expected loss is incomplete" in reason for reason in affected.unavailable_reasons)
    totals = result.totals
    assert totals.expected_loss_staying.fraction_of_book is None
    assert totals.net_benefit.fraction_of_book is None
    assert totals.exit_cost_exit_pockets.fraction_of_book == Decimal("0.011")
    assert totals.complete is False


def test_borrower_pocket_value_is_its_equity_from_the_analysis_graph():
    request = book(
        [
            {
                "position": {
                    "mode": "borrower",
                    "entity_id": "borrower",
                    "amount": "6000",
                    "collateral_amount": "4",
                    "debt_amount": "6000",
                    "ltv": "0.6",
                    "numeraire": "USDC",
                },
                "weight": "0.2",
            },
            {
                "position": {
                    "mode": "borrower",
                    "entity_id": "underwater",
                    "amount": "6000",
                    "collateral_amount": "1",
                    "debt_amount": "6000",
                    "ltv": "0.6",
                    "numeraire": "USDC",
                },
                "weight": "0.2",
            },
        ]
    )
    service = FakeService(
        {
            "borrower": response(expected_loss="40", collateral_value=Decimal(10000), debt_value=Decimal(6000)),
            "underwater": response(collateral_value=Decimal(5000), debt_value=Decimal(6000)),
        }
    )

    pockets = {pocket.entity_id: pocket for pocket in run_book(request, service).pockets}

    assert pockets["borrower"].pocket_value == Decimal(4000)
    assert pockets["borrower"].expected_loss.fraction_of_pocket == Decimal("0.01")
    assert pockets["borrower"].expected_loss.fraction_of_book == Decimal("0.002")
    assert pockets["underwater"].pocket_value is None
    assert pockets["underwater"].expected_loss.fraction_of_pocket is None
    assert "not positive" in pockets["underwater"].unavailable_reasons[0]


def test_usd_valued_lender_has_an_unknown_pocket_value_never_a_guess():
    request = book([{"position": lender("usd", numeraire="USD"), "weight": "1"}])

    result = run_book(request, FakeService({"usd": response()}))

    pocket = result.pockets[0]
    assert pocket.pocket_value is None
    assert pocket.expected_loss.amount == Decimal(1)
    assert pocket.expected_loss.fraction_of_book is None
    assert result.totals.expected_loss_staying.fraction_of_book is None
    assert result.totals.complete is False


def test_pockets_run_with_bounded_concurrency_and_their_own_scenarios():
    pockets = [{"position": lender(f"pocket-{index}"), "weight": "0.1"} for index in range(10)]
    pockets[0]["scenarios"] = [{"scenario_id": "oracle-failure", "severity": "0.10"}]
    service = FakeService(brief_outcomes(), delay=0.02)

    run_book(book(pockets), service, max_concurrency=3)

    assert 1 < service.peak <= 3
    by_entity = {request.position.entity_id: request for request in service.requests}
    assert by_entity["pocket-0"].scenarios[0].scenario_id.value == "oracle-failure"
    assert by_entity["pocket-1"].scenarios[0].scenario_id.value == "collateral-shock"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"weights": ["0.6", "0.5"]}, "sum to at most 1"),
        ({"weights": ["0", "0.5"]}, "greater than 0"),
        ({"weights": ["1.5"]}, "less than or equal to 1"),
        ({"no_scenarios": True}, "needs scenarios"),
        ({"book_value": "1000"}, "given together"),
        ({"pocket_ids": ["a", "a"]}, "unique"),
        ({"weights": ["0.01"] * 21}, "at most 20"),
    ],
)
def test_book_request_validation(change, message):
    weights = change.get("weights", ["0.5", "0.5"])
    pockets = [
        {"position": lender(f"pocket-{index}"), "weight": weight}
        for index, weight in enumerate(weights)
    ]
    for pocket, pocket_id in zip(pockets, change.get("pocket_ids", ()), strict=False):
        pocket["pocket_id"] = pocket_id
    payload: dict = {"pockets": pockets}
    if not change.get("no_scenarios"):
        payload["scenarios"] = SCENARIOS
    if "book_value" in change:
        payload["book_value"] = change["book_value"]

    with pytest.raises(ValidationError, match=message):
        BookRequest.model_validate(payload)


def test_book_weights_are_exact_decimals_never_binary_floats():
    with pytest.raises(ValidationError):
        BookRequest.model_validate(
            {"pockets": [{"position": lender("a"), "weight": 0.1}], "scenarios": SCENARIOS}
        )
    # Ten exact tenths sum to exactly one.
    assert sum(pocket.weight for pocket in ten_pockets().pockets) == Decimal(1)
