"""Lender bad debt in an over-collateralised Morpho market after a collateral price fall.

A lender in an over-collateralised market does not lose when collateral falls;
it loses only the debt that the collateral no longer covers (bad debt), which
Morpho Blue socialises across the market's suppliers when it is realised.

Model (conservative): the collateral price gaps down instantly by the shock `s`
(relative to the loan asset) and no liquidation executes in between. Borrower
i, with debt `d_i` and health factor `HF_i = collateral_value_i * LLTV / d_i`,
has collateral/debt ratio `r_i = HF_i / LLTV`; after the shock it is
`(1 - s) * r_i` and its bad debt is

    bad_debt_i = d_i * max(0, 1 - (1 - s) * r_i)

The supplier loss fraction of the market is `sum(bad_debt_i) / total_supply`.

Borrowers are observed through Morpho's position list (top positions by
borrow shares). Debt the list does not cover (truncated list, borrowers without
a health factor, or no list at all) is priced at the worst case a healthy
borrower can be in: exactly at LLTV, i.e. `r = 1 / LLTV`, which gives the bound

    bad_debt_fraction_at_lltv(s) = max(0, 1 - (1 - s) / LLTV)

per unit of uncovered debt. Without any position list the whole market is
priced that way, so the supplier loss fraction is that bound times the
utilization (`total_borrow / total_supply`).

Ratios are floored to `RATIO_QUANTUM` when the distribution is built (a lower
ratio can only raise the bad debt), so the compact distribution never
understates the exact per-borrower figure.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal


ZERO = Decimal(0)
ONE = Decimal(1)
RATIO_QUANTUM = Decimal("0.001")
# Ratios above this are pooled: such a borrower only becomes bad debt in a
# shock above 99%, and pooling at the cap (a floor) keeps the bound conservative.
RATIO_CAP = Decimal(100)

METHOD_POSITIONS = "borrower_positions"
METHOD_WORST_CASE = "worst_case_at_lltv"
METHOD_LLTV_UNKNOWN = "worst_case_lltv_unknown"


@dataclass(frozen=True, slots=True)
class BorrowerDistribution:
    """Compact debt distribution of one market's observed borrowers, in loan-asset units.

    `buckets` holds `(ratio, debt, borrowers)` sorted by ratio, where `ratio` is
    the collateral/debt ratio (`HF / LLTV`) floored to `RATIO_QUANTUM`, `debt`
    the summed debt of the borrowers in that bucket and `borrowers` their count.
    """

    market_id: str
    buckets: tuple[tuple[Decimal, Decimal, int], ...]
    covered_debt: Decimal
    borrower_count: int
    # Borrowers with debt but no health factor: their debt is left uncovered.
    unrated_borrowers: int
    # False when the position list was truncated or an item could not be parsed.
    complete: bool


def build_distribution(
    market_id: str,
    borrowers: Iterable[tuple[Decimal, Decimal | None]],
    lltv: Decimal,
    *,
    complete: bool,
) -> BorrowerDistribution:
    """Bucket `(debt, health_factor)` pairs; zero-debt rows are skipped."""
    if lltv <= ZERO:
        raise ValueError("lltv must be positive")
    buckets: dict[Decimal, tuple[Decimal, int]] = {}
    covered = ZERO
    count = 0
    unrated = 0
    for debt, health_factor in borrowers:
        if debt <= ZERO:
            continue
        if health_factor is None or not health_factor.is_finite() or health_factor < ZERO:
            unrated += 1
            continue
        ratio = min(RATIO_CAP, health_factor / lltv).quantize(RATIO_QUANTUM, ROUND_FLOOR)
        bucket_debt, bucket_count = buckets.get(ratio, (ZERO, 0))
        buckets[ratio] = (bucket_debt + debt, bucket_count + 1)
        covered += debt
        count += 1
    return BorrowerDistribution(
        market_id=market_id.lower(),
        buckets=tuple(
            (ratio, debt, borrowers) for ratio, (debt, borrowers) in sorted(buckets.items())
        ),
        covered_debt=covered,
        borrower_count=count,
        unrated_borrowers=unrated,
        complete=complete,
    )


@dataclass(frozen=True, slots=True)
class BadDebtEstimate:
    method: str
    # Collateral fall relative to the loan asset that was applied.
    shock: Decimal
    lltv: Decimal | None
    total_supply: Decimal | None
    total_borrow: Decimal | None
    # Loan-asset units; None when the market totals are unknown.
    bad_debt: Decimal | None
    supplier_loss_fraction: Decimal
    # Debt priced at the LLTV worst case because no borrower position covered it.
    uncovered_debt: Decimal | None
    borrower_count: int | None
    borrowers_underwater: int | None
    # Observed borrowers: Σ debt_i·(1 − (1 − s)·r_i) over the underwater ones.
    observed_bad_debt: Decimal | None

    @property
    def is_bound(self) -> bool:
        """True when the figure is a worst-case bound rather than a position-based estimate."""
        return self.method != METHOD_POSITIONS


def bad_debt_fraction_at_lltv(shock: Decimal, lltv: Decimal) -> Decimal:
    """Share of debt that is bad when every borrower sits exactly at LLTV."""
    return max(ZERO, ONE - (ONE - shock) / lltv)


def borrower_bad_debt(debt: Decimal, ratio: Decimal, shock: Decimal) -> Decimal:
    """`debt * max(0, 1 - (1 - shock) * ratio)` for one borrower or bucket."""
    return debt * max(ZERO, ONE - (ONE - shock) * ratio)


def estimate_bad_debt(
    shock: Decimal,
    *,
    lltv: Decimal | None,
    total_supply: Decimal | None,
    total_borrow: Decimal | None,
    utilization: Decimal | None = None,
    distribution: BorrowerDistribution | None = None,
) -> BadDebtEstimate:
    """Supplier loss fraction of one market after a collateral fall of `shock`."""
    shock = min(ONE, max(ZERO, shock))
    if lltv is None or lltv <= ZERO:
        # LLTV unknown: the worst case is LLTV -> 1, so every unit of debt loses `shock`.
        worst = shock
        method = METHOD_LLTV_UNKNOWN
        distribution = None
    else:
        worst = bad_debt_fraction_at_lltv(shock, lltv)
        method = METHOD_WORST_CASE if distribution is None else METHOD_POSITIONS

    if distribution is None:
        observed = None
        underwater = None
        borrowers = None
        uncovered = total_borrow
    else:
        observed = ZERO
        underwater = 0
        for ratio, debt, count in distribution.buckets:
            loss = borrower_bad_debt(debt, ratio, shock)
            if loss > ZERO:
                observed += loss
                underwater += count
        borrowers = distribution.borrower_count
        uncovered = (
            None if total_borrow is None else max(ZERO, total_borrow - distribution.covered_debt)
        )

    if total_supply is not None and total_supply > ZERO and total_borrow is not None:
        bad = (observed or ZERO) + worst * (uncovered or ZERO)
        fraction = min(ONE, bad / total_supply)
    else:
        # Totals unknown: bound by utilization, or by full utilization when that is unknown too.
        bad = None
        share = utilization if utilization is not None and ZERO <= utilization <= ONE else ONE
        fraction = min(ONE, worst * share)
        if distribution is not None:
            method = METHOD_WORST_CASE
    return BadDebtEstimate(
        method=method,
        shock=shock,
        lltv=lltv,
        total_supply=total_supply,
        total_borrow=total_borrow,
        bad_debt=bad,
        supplier_loss_fraction=fraction,
        uncovered_debt=uncovered,
        borrower_count=borrowers,
        borrowers_underwater=underwater,
        observed_bad_debt=observed,
    )
