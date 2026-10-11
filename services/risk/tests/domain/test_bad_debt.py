from decimal import Decimal

import pytest

from app.domain.bad_debt import (
    METHOD_LLTV_UNKNOWN,
    METHOD_POSITIONS,
    METHOD_WORST_CASE,
    bad_debt_fraction_at_lltv,
    build_distribution,
    estimate_bad_debt,
)


D = Decimal
# LLTV 0.8 keeps HF / LLTV exact, so ratios are not floored in these examples.
LLTV = D("0.8")
SUPPLY = D("1000")
BORROW = D("800")


def book(*rows, complete=True, lltv=LLTV):
    """Rows of (debt, health factor); a health factor of None is an unrated borrower."""
    return build_distribution(
        "0xABC",
        [(D(debt), None if hf is None else D(hf)) for debt, hf in rows],
        lltv,
        complete=complete,
    )


def estimate(shock, distribution=None, **overrides):
    values = {"lltv": LLTV, "total_supply": SUPPLY, "total_borrow": BORROW}
    values.update(overrides)
    return estimate_bad_debt(D(shock), distribution=distribution, **values)


def test_no_bad_debt_when_every_borrower_keeps_collateral_above_debt():
    # HF 1.5 at LLTV 0.8: collateral/debt 1.875; after a 40% fall 1.125 > 1.
    result = estimate("0.40", book(("500", "1.5"), ("300", "2.0")))

    assert result.method == METHOD_POSITIONS
    assert result.bad_debt == 0
    assert result.supplier_loss_fraction == 0
    assert result.borrowers_underwater == 0
    assert result.borrower_count == 2
    assert result.uncovered_debt == 0
    assert result.is_bound is False


def test_partial_bad_debt_counts_only_borrowers_whose_collateral_falls_below_debt():
    # Borrower 1: HF 0.88 -> ratio 1.1; after 20% fall 0.88 -> bad debt 400 * 0.12 = 48.
    # Borrower 2: HF 1.2 -> ratio 1.5; after 20% fall 1.2 -> no bad debt.
    result = estimate("0.20", book(("400", "0.88"), ("400", "1.2")))

    assert result.bad_debt == D("48.000")
    assert result.supplier_loss_fraction == D("48.000") / SUPPLY
    assert result.borrowers_underwater == 1
    assert result.borrower_count == 2


def test_bad_debt_formula_matches_definition_per_borrower():
    shock = D("0.25")
    rows = [("100", "0.9"), ("250", "1.0"), ("50", "1.6"), ("400", "0.84")]
    expected = sum(
        (
            D(debt) * max(D(0), 1 - (1 - shock) * (D(hf) / LLTV))
            for debt, hf in rows
        ),
        D(0),
    )

    result = estimate(shock, book(*rows))

    assert result.bad_debt == expected
    assert expected > 0


def test_all_at_lltv_bound_equals_formula_without_positions():
    shock = D("0.30")
    bound = 1 - (1 - shock) / LLTV  # 0.125

    result = estimate(shock)

    assert bad_debt_fraction_at_lltv(shock, LLTV) == bound
    assert result.method == METHOD_WORST_CASE
    assert result.is_bound is True
    assert result.bad_debt == bound * BORROW
    # Supplier loss = bound x utilization (800 / 1000).
    assert result.supplier_loss_fraction == bound * D("0.8")
    assert result.borrower_count is None


def test_positions_all_at_lltv_reproduce_the_worst_case_bound():
    # HF 1.0 means collateral/debt is exactly 1 / LLTV: the bound's assumption.
    shock = D("0.30")

    observed = estimate(shock, book(("800", "1.0")))
    bound = estimate(shock)

    assert observed.method == METHOD_POSITIONS
    assert observed.supplier_loss_fraction == bound.supplier_loss_fraction


def test_bound_is_zero_while_the_fall_stays_inside_the_lltv_cushion():
    # 1 - LLTV = 20% cushion: a 15% fall leaves even LLTV borrowers solvent.
    assert bad_debt_fraction_at_lltv(D("0.15"), LLTV) == 0
    assert estimate("0.15").supplier_loss_fraction == 0


def test_uncovered_debt_is_priced_at_the_lltv_worst_case():
    # Positions cover 600 of the 800 debt (truncated list): the other 200 sit at LLTV.
    shock = D("0.30")
    result = estimate(shock, book(("600", "2.0"), complete=False))

    assert result.method == METHOD_POSITIONS
    assert result.uncovered_debt == D("200")
    assert result.observed_bad_debt == 0
    assert result.bad_debt == D("200") * bad_debt_fraction_at_lltv(shock, LLTV)
    assert result.supplier_loss_fraction == result.bad_debt / SUPPLY


def test_borrowers_without_health_factor_and_zero_debt_rows_are_skipped():
    distribution = book(("500", "2.0"), ("300", None), ("0", "0.5"))

    assert distribution.borrower_count == 1
    assert distribution.unrated_borrowers == 1
    assert distribution.covered_debt == D("500")
    # The unrated borrower's 300 is uncovered and priced at the worst case.
    result = estimate("0.30", distribution)
    assert result.uncovered_debt == D("300")
    assert result.bad_debt == D("300") * bad_debt_fraction_at_lltv(D("0.30"), LLTV)


def test_covered_debt_above_market_total_leaves_nothing_uncovered():
    result = estimate("0.30", book(("900", "2.0")))

    assert result.uncovered_debt == 0
    assert result.bad_debt == 0


def test_ratio_flooring_never_understates_bad_debt():
    lltv = D("0.86")
    rows = [(D("100"), D("1.0123456")), (D("100"), D("0.9555555"))]
    exact = sum(
        (debt * max(D(0), 1 - D("0.85") * hf / lltv) for debt, hf in rows), D(0)
    )

    result = estimate_bad_debt(
        D("0.15"),
        lltv=lltv,
        total_supply=SUPPLY,
        total_borrow=D("200"),
        distribution=build_distribution("0xabc", rows, lltv, complete=True),
    )

    assert result.bad_debt >= exact
    assert result.bad_debt - exact < D("0.001") * D("200")


def test_unknown_lltv_uses_the_lltv_one_bound():
    result = estimate("0.30", lltv=None)

    assert result.method == METHOD_LLTV_UNKNOWN
    assert result.supplier_loss_fraction == D("0.30") * D("0.8")


def test_unknown_totals_fall_back_to_utilization_then_to_full_utilization():
    with_utilization = estimate("0.30", total_supply=None, total_borrow=None, utilization=D("0.5"))
    without = estimate("0.30", total_supply=None, total_borrow=None)

    bound = bad_debt_fraction_at_lltv(D("0.30"), LLTV)
    assert with_utilization.supplier_loss_fraction == bound * D("0.5")
    assert without.supplier_loss_fraction == bound
    assert with_utilization.bad_debt is None


def test_total_loss_is_capped_at_the_supply():
    result = estimate("1", book(("800", "1.0")))

    assert result.bad_debt == BORROW
    assert result.supplier_loss_fraction == D("0.8")
    assert estimate("1", total_supply=D("100")).supplier_loss_fraction == 1


def test_distribution_rejects_non_positive_lltv():
    with pytest.raises(ValueError):
        build_distribution("0xabc", [], D(0), complete=True)
