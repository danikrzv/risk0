"""Book view: run the per-position analysis for every pocket of a book and aggregate.

The challenge frames the decision as a book split into pockets ("ten pockets of
10% each; one is affected; unwinding it costs 11% in slippage"). Each pocket is
a position with a weight (its fraction of the book). Every pocket runs through
the unchanged `AnalysisService.analyze`, with bounded concurrency, and keeps its
own STAY / EXIT / UNAVAILABLE recommendation.

Units
- Per-pocket amounts (expected loss, exit cost, their difference) are in the
  pocket's own numeraire, exactly as the analysis reports them.
- `pocket_value` is what the pocket is worth in its numeraire: a lender's
  deposit `amount` (the lender numeraire is the deposited asset, so the amount
  is the value), or a borrower's equity `collateral_value - debt_value` from
  the analysis graph's position node. A lender valued in `USD` has a deposit in
  token units, so its value is unknown here and stays null (never guessed).
- `fraction_of_pocket = amount / pocket_value` and
  `fraction_of_book = fraction_of_pocket * weight`. Fractions are numeraire-free,
  so they add up across pockets in different assets.

Totals cover the decided (STAY / EXIT) pockets; `complete` is false when a
pocket is UNAVAILABLE, and a total whose inputs are unknown is null with a
warning (an unknown value is never treated as zero). Total amounts are
`fraction_of_book * book_value` in the book numeraire when `book_value` is
given; otherwise the sum of the pocket amounts when every counted pocket shares
one numeraire; otherwise null.

- expected_loss_staying: expected 30-day loss if every pocket stays.
- exit_cost_exit_pockets: cost of exiting the pockets whose decision is EXIT.
- expected_loss_stay_pockets: expected loss of the pockets that stay.
- recommended_cost: exit_cost_exit_pockets + expected_loss_stay_pockets.
- net_benefit: expected_loss_staying - recommended_cost (what following the
  recommendations saves, in expectation, against staying everywhere).

Pockets are ranked by `exit_advantage = expected_loss - exit_cost` as a
fraction of the book (largest first: the pocket where exiting saves most), then
decided pockets without a book fraction, then UNAVAILABLE pockets. A pocket
whose analysis raises becomes UNAVAILABLE with a sanitized reason; the book
itself never fails because of one pocket.
"""

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.analysis.service import AnalysisError
from app.contracts.models import (
    AnalysisRequest,
    AnalysisResponse,
    BookAmount,
    BookPocketInput,
    BookPocketResult,
    BookRequest,
    BookResponse,
    BookTotal,
    BookTotals,
    ConfidenceLevel,
    FreshnessStatus,
    PositionInput,
    PositionMode,
    RecommendationDecision,
    RiskNodeType,
)
from app.domain.exit import NUMERAIRE_PLACES


logger = logging.getLogger("risk0.book")

MAX_CONCURRENCY = 4
ZERO = Decimal(0)
POCKET_FAILED = "The analysis of this pocket failed; no decision is given for it."
USD = "USD"


class PocketAnalyzer(Protocol):
    def analyze(self, request: AnalysisRequest) -> AnalysisResponse: ...


@dataclass(frozen=True, slots=True)
class _Outcome:
    analysis: AnalysisResponse | None
    error: str | None


def run_book(
    request: BookRequest,
    service: PocketAnalyzer,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_concurrency: int = MAX_CONCURRENCY,
) -> BookResponse:
    pockets = request.pockets
    workers = max(1, min(max_concurrency, len(pockets)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="risk0-book") as pool:
        outcomes = list(pool.map(lambda pocket: _analyze(pocket, request, service), pockets))
    results = [
        _pocket_result(index, pocket, outcome)
        for index, (pocket, outcome) in enumerate(zip(pockets, outcomes, strict=True))
    ]
    ranked = sorted(range(len(results)), key=lambda index: _rank_key(results[index], index))
    ordered = tuple(
        results[index].model_copy(update={"rank": rank})
        for rank, index in enumerate(ranked, start=1)
    )
    return BookResponse(
        pockets=ordered,
        totals=_totals(ordered, request),
        generated_at=clock(),
    )


def _analyze(pocket: BookPocketInput, book: BookRequest, service: PocketAnalyzer) -> _Outcome:
    scenarios = pocket.scenarios if pocket.scenarios is not None else book.scenarios
    try:
        analysis = service.analyze(AnalysisRequest(position=pocket.position, scenarios=scenarios))
        return _Outcome(analysis=analysis, error=None)
    except AnalysisError as error:
        return _Outcome(analysis=None, error=error.public_message)
    except Exception as error:  # never a stack trace, URL, or provider body
        logger.error("book pocket analysis failed: %s", type(error).__name__)
        return _Outcome(analysis=None, error=POCKET_FAILED)


def _pocket_result(index: int, pocket: BookPocketInput, outcome: _Outcome) -> BookPocketResult:
    position = pocket.position
    common = {
        "pocket_id": pocket.pocket_id or f"pocket-{index + 1}",
        "rank": index + 1,
        "entity_id": position.entity_id,
        "mode": position.mode,
        "numeraire": position.numeraire,
        "weight": pocket.weight,
    }
    analysis = outcome.analysis
    if analysis is None:
        reason = outcome.error or POCKET_FAILED
        return BookPocketResult(
            **common,
            decision=RecommendationDecision.UNAVAILABLE,
            confidence=ConfidenceLevel.UNAVAILABLE,
            dominant_dependency=None,
            triggers=(),
            pocket_value=None,
            expected_loss=None,
            exit_cost=None,
            exit_advantage=None,
            break_even=None,
            unavailable_reasons=(reason,),
            explanation=f"UNAVAILABLE: {reason}",
            analyzed_at=None,
        )

    recommendation = analysis.recommendation
    reasons: list[str] = []
    pocket_value, value_reason = _pocket_value(position, analysis)
    if value_reason:
        reasons.append(value_reason)
    decided = recommendation.decision is not RecommendationDecision.UNAVAILABLE
    if not decided:
        reasons.insert(0, recommendation.explanation)

    def amount(value: Decimal) -> BookAmount:
        return _amount(value, pocket_value, pocket.weight)

    exit_known = analysis.exit_estimate.freshness is not FreshnessStatus.EXPIRED
    exit_cost = amount(recommendation.exit_cost) if exit_known else None
    if not exit_known and decided:  # defensive: a decision always has a current exit estimate
        reasons.append("the exit cost is unknown (no current exit estimate)")

    unknown = _unknown_probability(analysis)
    expected_loss = None
    if decided and not unknown:
        expected_loss = amount(recommendation.expected_loss)
    elif decided:
        reasons.append(
            "the expected loss is incomplete: no 30-day probability for "
            f"{', '.join(unknown)}"
        )
    advantage = None
    if expected_loss is not None and exit_cost is not None:
        advantage = amount(expected_loss.amount - exit_cost.amount)

    return BookPocketResult(
        **common,
        decision=recommendation.decision,
        confidence=recommendation.confidence,
        dominant_dependency=recommendation.dominant_dependency,
        triggers=recommendation.triggers,
        pocket_value=pocket_value,
        expected_loss=expected_loss,
        exit_cost=exit_cost,
        exit_advantage=advantage,
        break_even=recommendation.break_even,
        unavailable_reasons=tuple(dict.fromkeys(reasons)),
        explanation=recommendation.explanation,
        analyzed_at=analysis.analyzed_at,
    )


def _pocket_value(
    position: PositionInput, analysis: AnalysisResponse
) -> tuple[Decimal | None, str | None]:
    if position.mode is PositionMode.LENDER:
        if position.numeraire.strip().upper() == USD:
            return None, (
                "the pocket value is unknown: the deposit is in token units and the "
                "numeraire is USD, so fractions of the pocket are not computed"
            )
        return position.amount, None
    node = next(
        (item for item in analysis.graph.nodes if item.node_type is RiskNodeType.POSITION), None
    )
    collateral = _decimal(None if node is None else node.metrics.get("collateral_value"))
    debt = _decimal(None if node is None else node.metrics.get("debt_value"))
    if collateral is None or debt is None:
        return None, (
            "the pocket value is unknown: the collateral or debt value is not available "
            "in the numeraire"
        )
    equity = collateral - debt
    if equity <= ZERO:
        return None, "the pocket value is not positive (debt is at least the collateral value)"
    return _quantize(equity), None


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def _unknown_probability(analysis: AnalysisResponse) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            result.scenario_id
            for result in analysis.scenario_results
            if result.probability.central is None and result.impact != ZERO
        )
    )


def _amount(value: Decimal, pocket_value: Decimal | None, weight: Decimal) -> BookAmount:
    if pocket_value is None:
        return BookAmount(amount=value, fraction_of_pocket=None, fraction_of_book=None)
    fraction = _quantize(value / pocket_value)
    return BookAmount(
        amount=value,
        fraction_of_pocket=fraction,
        fraction_of_book=_quantize(fraction * weight),
    )


def _rank_key(result: BookPocketResult, index: int) -> tuple[int, Decimal, int]:
    if result.decision is RecommendationDecision.UNAVAILABLE:
        return (2, ZERO, index)
    advantage = result.exit_advantage
    if advantage is None or advantage.fraction_of_book is None:
        return (1, ZERO, index)
    return (0, -advantage.fraction_of_book, index)


def _totals(pockets: Sequence[BookPocketResult], request: BookRequest) -> BookTotals:
    decided = [p for p in pockets if p.decision is not RecommendationDecision.UNAVAILABLE]
    exits = [p for p in decided if p.decision is RecommendationDecision.EXIT]
    stays = [p for p in decided if p.decision is RecommendationDecision.STAY]
    unavailable = [p for p in pockets if p.decision is RecommendationDecision.UNAVAILABLE]
    warnings: list[str] = []

    numeraire = request.numeraire
    shared = None
    if request.book_value is None:
        symbols = {p.numeraire.casefold() for p in decided}
        if len(symbols) == 1:
            shared = decided[0].numeraire
            numeraire = shared
        elif decided:
            warnings.append(
                "Pockets use different numeraires and no book value was given, so book "
                "totals are reported as fractions of the book only."
            )

    def total(items: Sequence[BookAmount | None], label: str) -> BookTotal:
        if any(item is None for item in items):
            warnings.append(f"The {label} is unknown: at least one counted pocket lacks it.")
            return BookTotal(amount=None, fraction_of_book=None)
        known = [item for item in items if item is not None]
        fractions = [item.fraction_of_book for item in known]
        fraction = None
        if all(value is not None for value in fractions):
            fraction = _quantize(sum((value for value in fractions if value is not None), ZERO))
        else:
            warnings.append(
                f"The {label} as a fraction of the book is unknown: a pocket value is unknown."
            )
        if request.book_value is not None:
            amount = None if fraction is None else _quantize(fraction * request.book_value)
        elif shared is not None:
            amount = _quantize(sum((item.amount for item in known), ZERO))
        else:
            amount = None
        return BookTotal(amount=amount, fraction_of_book=fraction)

    staying = total([p.expected_loss for p in decided], "expected loss if staying in every pocket")
    exit_cost = total([p.exit_cost for p in exits], "exit cost of the EXIT pockets")
    stay_loss = total([p.expected_loss for p in stays], "expected loss of the STAY pockets")
    recommended = BookTotal(
        amount=_add(exit_cost.amount, stay_loss.amount),
        fraction_of_book=_add(exit_cost.fraction_of_book, stay_loss.fraction_of_book),
    )
    net = BookTotal(
        amount=_subtract(staying.amount, recommended.amount),
        fraction_of_book=_subtract(staying.fraction_of_book, recommended.fraction_of_book),
    )
    if unavailable:
        warnings.insert(
            0,
            f"{len(unavailable)} pocket{'s are' if len(unavailable) != 1 else ' is'} "
            "UNAVAILABLE and excluded from the totals; the totals are incomplete.",
        )
    allocated = sum((p.weight for p in pockets), ZERO)
    return BookTotals(
        numeraire=numeraire,
        expected_loss_staying=staying,
        exit_cost_exit_pockets=exit_cost,
        expected_loss_stay_pockets=stay_loss,
        recommended_cost=recommended,
        net_benefit=net,
        allocated_weight=allocated,
        stay_weight=sum((p.weight for p in stays), ZERO),
        exit_weight=sum((p.weight for p in exits), ZERO),
        unavailable_weight=sum((p.weight for p in unavailable), ZERO),
        stay_count=len(stays),
        exit_count=len(exits),
        unavailable_count=len(unavailable),
        complete=not unavailable and all(
            item.fraction_of_book is not None
            for item in (staying, exit_cost, stay_loss, recommended, net)
        ),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _add(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return None if left is None or right is None else left + right


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return None if left is None or right is None else left - right


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(NUMERAIRE_PLACES)
