"""Deterministic lender solvency model: liquidation zone, scenarios A/B, vault aggregation."""

from decimal import Decimal, localcontext

import pytest

from app.domain.solvency import (
    R_PANIC,
    SolvencyScenario,
    lender_solvency,
    liquidation_zone,
    market_solvency,
    vault_solvency,
)


D = Decimal
LLTV = D("0.86")


def close(actual: Decimal | None, expected: Decimal) -> bool:
    assert actual is not None
    return abs(actual - expected) < D("1e-40")


def exact(compute):
    """Evaluate an expectation at the model's precision."""
    with localcontext() as context:
        context.prec = 80
        return compute()


# Liquidation zone (Q)


def test_liquidation_zone_counts_borrowers_at_or_above_95_percent_of_lltv() -> None:
    # HF <= 1/0.95 (about 1.0526) means LTV >= 95% of LLTV.
    zone = liquidation_zone(
        [
            (D("10"), D("100"), D("1.05")),  # in zone
            (D("4"), D("50"), D("1.0526")),  # in zone, just inside
            (D("7"), D("70"), D("1.06")),  # outside
            (D("3"), D("0"), None),  # no debt: ignored
        ],
        lltv=LLTV,
        complete=True,
        total_borrow=D("220"),
        p_contract=D("100"),
    )
    assert zone is not None
    assert zone.collateral == D("14")
    assert zone.borrowers_in_zone == 2
    assert zone.uncovered_debt == D("0")
    assert zone.bound is False


def test_liquidation_zone_prices_uncovered_debt_at_lltv_when_the_list_is_truncated() -> None:
    zone = liquidation_zone(
        [(D("10"), D("100"), D("1.5"))],
        lltv=D("0.8"),
        complete=False,
        total_borrow=D("500"),
        p_contract=D("50"),
    )
    assert zone is not None
    # 400 of debt is uncovered; at LLTV its collateral is 400 / (50 * 0.8) = 10.
    assert zone.uncovered_debt == D("400")
    assert zone.collateral == D("10")
    assert zone.bound is True


def test_liquidation_zone_treats_unrated_borrowers_as_uncovered() -> None:
    zone = liquidation_zone(
        [(D("10"), D("100"), D("2")), (D("5"), D("40"), None)],
        lltv=D("0.8"),
        complete=True,
        total_borrow=D("140"),
        p_contract=D("10"),
    )
    assert zone is not None
    assert zone.uncovered_debt == D("40")
    assert zone.collateral == D("5")  # 40 / (10 * 0.8)
    assert zone.bound is True


def test_liquidation_zone_is_unknown_when_an_incomplete_list_cannot_be_bounded() -> None:
    assert (
        liquidation_zone(
            [(D("10"), D("100"), D("1.5"))],
            lltv=LLTV,
            complete=False,
            total_borrow=None,
            p_contract=D("50"),
        )
        is None
    )
    assert (
        liquidation_zone(
            [(D("10"), D("100"), D("1.5"))],
            lltv=LLTV,
            complete=False,
            total_borrow=D("500"),
            p_contract=None,
        )
        is None
    )


# Market scenarios


def test_scenario_a_when_q_exceeds_half_the_exit_depth() -> None:
    result = market_solvency(
        q=D("60"), r=D("100"), lltv=LLTV, p_contract=D("2000"), p_real=D("1990"),
        total_supply=D("1000000"),
    )
    assert result.scenario is SolvencyScenario.A_SYSTEMIC
    assert result.q_over_half_r == D("1.2")
    assert result.supplier_loss_fraction == D(1) - R_PANIC
    assert result.bad_debt is None


def test_scenario_b_with_zero_bad_debt() -> None:
    # R = 1000: factor (1000/1010)^2 = 0.980296, recovered 19605.9 > debt issued 17200.
    result = market_solvency(
        q=D("10"), r=D("1000"), lltv=LLTV, p_contract=D("2000"), p_real=D("2000"),
        total_supply=D("1000000"),
    )
    assert result.scenario is SolvencyScenario.B_DEGRADED
    assert result.debt_issued == D("17200")
    assert close(result.slippage_factor, exact(lambda: (D("1000") / D("1010")) ** 2))
    assert result.recovered_value > result.debt_issued
    assert result.bad_debt == D("0")
    assert result.supplier_loss_fraction == D("0")


def test_scenario_b_with_positive_bad_debt() -> None:
    result = market_solvency(
        q=D("40"), r=D("100"), lltv=LLTV, p_contract=D("2000"), p_real=D("1800"),
        total_supply=D("1000000"),
    )
    assert result.scenario is SolvencyScenario.B_DEGRADED
    debt_issued = D("40") * D("2000") * LLTV  # 68800
    recovered = exact(lambda: D("40") * D("1800") * (D("100") / D("140")) ** 2)  # 36734.69
    assert result.debt_issued == debt_issued
    assert close(result.recovered_value, recovered)
    assert close(result.bad_debt, exact(lambda: debt_issued - recovered))
    assert result.bad_debt > 0
    assert close(result.supplier_loss_fraction, exact(lambda: (debt_issued - recovered) / D("1000000")))


def test_q_equal_to_half_r_is_scenario_b() -> None:
    result = market_solvency(
        q=D("50"), r=D("100"), lltv=LLTV, p_contract=D("1"), p_real=D("1"),
        total_supply=D("1000"),
    )
    assert result.q_over_half_r == D("1")
    assert result.scenario is SolvencyScenario.B_DEGRADED


def test_empty_liquidation_zone_is_healthy_without_other_inputs() -> None:
    result = market_solvency(
        q=D("0"), r=None, lltv=LLTV, p_contract=None, p_real=None, total_supply=D("1000"),
    )
    assert result.scenario is SolvencyScenario.B_DEGRADED
    assert result.bad_debt == D("0")
    assert result.supplier_loss_fraction == D("0")


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"q": None}, "q"),
        ({"r": None}, "r"),
        ({"p_contract": None}, "p_contract"),
        ({"p_real": None}, "p_real"),
        ({"lltv": None}, "lltv"),
    ],
)
def test_missing_inputs_make_the_market_unavailable(overrides: dict, missing: str) -> None:
    inputs = {
        "q": D("10"), "r": D("1000"), "lltv": LLTV, "p_contract": D("2000"),
        "p_real": D("2000"), "total_supply": D("1000000"),
    }
    inputs.update(overrides)
    result = market_solvency(**inputs)
    assert result.scenario is SolvencyScenario.UNAVAILABLE
    assert missing in result.missing
    assert result.supplier_loss_fraction is None
    assert result.bad_debt is None


def test_bad_debt_without_total_supply_is_unavailable() -> None:
    result = market_solvency(
        q=D("40"), r=D("100"), lltv=LLTV, p_contract=D("2000"), p_real=D("1800"),
        total_supply=None,
    )
    assert result.scenario is SolvencyScenario.UNAVAILABLE
    assert "total_supply" in result.missing
    # The market-level bad debt is still reported; only the supplier share is unknown.
    assert result.bad_debt is not None


def test_r_panic_is_configurable() -> None:
    result = market_solvency(
        q=D("60"), r=D("100"), lltv=LLTV, p_contract=D("1"), p_real=D("1"),
        total_supply=D("1000"), r_panic=D("0.5"),
    )
    assert result.supplier_loss_fraction == D("0.5")


def test_no_floats_reach_the_model() -> None:
    with pytest.raises(TypeError):
        market_solvency(
            q=10.0, r=D("1000"), lltv=LLTV, p_contract=D("1"), p_real=D("1"),  # type: ignore[arg-type]
            total_supply=D("1000"),
        )


# Position and vault


def test_market_lender_expected_loss_is_bad_debt_times_w_pool() -> None:
    market = market_solvency(
        q=D("40"), r=D("100"), lltv=LLTV, p_contract=D("2000"), p_real=D("1800"),
        total_supply=D("1000000"),
    )
    position = lender_solvency(
        x=D("10000"), x_in_pool_units=D("10000"), pool_total=D("1000000"), loss_fraction=market.supplier_loss_fraction,
        scenario=market.scenario,
    )
    assert position.w_pool == D("0.01")
    assert close(position.expected_loss, exact(lambda: market.bad_debt * D("0.01")))
    assert position.vulnerable is True


def test_scenario_a_position_loses_x_times_one_minus_r_panic() -> None:
    position = lender_solvency(
        x=D("10000"), x_in_pool_units=D("10000"), pool_total=D("1000000"),
        loss_fraction=D(1) - R_PANIC, scenario=SolvencyScenario.A_SYSTEMIC,
    )
    assert position.expected_loss == D("7000")


def test_unavailable_position_has_no_expected_loss() -> None:
    position = lender_solvency(
        x=D("10000"), x_in_pool_units=None, pool_total=D("1000000"), loss_fraction=None,
        scenario=SolvencyScenario.UNAVAILABLE,
    )
    assert position.expected_loss is None
    assert position.w_pool is None
    assert position.vulnerable is None


def test_vault_aggregates_market_losses_by_its_supply_share() -> None:
    healthy = market_solvency(
        q=D("0"), r=None, lltv=LLTV, p_contract=None, p_real=None, total_supply=D("500"),
    )
    impaired = market_solvency(
        q=D("40"), r=D("100"), lltv=LLTV, p_contract=D("2000"), p_real=D("1800"),
        total_supply=D("1000000"),
    )
    vault = vault_solvency(
        total_assets=D("300000"),
        holdings=[(D("100000"), healthy), (D("200000"), impaired)],
        unmodelled=False,
    )
    # Vault loss in the impaired market = its supply share (0.2) times the market bad debt.
    expected_vault_loss = exact(lambda: D("0.2") * impaired.bad_debt)
    assert vault.scenario is SolvencyScenario.B_DEGRADED
    assert close(vault.bad_debt, expected_vault_loss)
    assert close(vault.loss_fraction, exact(lambda: expected_vault_loss / D("300000")))

    position = lender_solvency(
        x=D("3000"), x_in_pool_units=D("3000"), pool_total=D("300000"),
        loss_fraction=vault.loss_fraction, scenario=vault.scenario,
    )
    assert position.w_pool == D("0.01")
    assert close(position.expected_loss, exact(lambda: expected_vault_loss * D("0.01")))


def test_vault_with_a_systemic_market_takes_the_panic_loss_on_that_share() -> None:
    systemic = market_solvency(
        q=D("60"), r=D("100"), lltv=LLTV, p_contract=D("1"), p_real=D("1"), total_supply=D("1000"),
    )
    healthy = market_solvency(
        q=D("0"), r=None, lltv=LLTV, p_contract=None, p_real=None, total_supply=D("1000"),
    )
    vault = vault_solvency(
        total_assets=D("1000"), holdings=[(D("400"), systemic), (D("600"), healthy)], unmodelled=False,
    )
    assert vault.scenario is SolvencyScenario.A_SYSTEMIC
    assert vault.bad_debt == D("400") * (D(1) - R_PANIC)
    assert vault.loss_fraction == D("0.28")


def test_vault_is_unavailable_when_a_market_or_allocation_is_unknown() -> None:
    healthy = market_solvency(
        q=D("0"), r=None, lltv=LLTV, p_contract=None, p_real=None, total_supply=D("1000"),
    )
    unknown = market_solvency(
        q=None, r=None, lltv=LLTV, p_contract=None, p_real=None, total_supply=D("1000"),
    )
    assert vault_solvency(
        total_assets=D("1000"), holdings=[(D("500"), healthy), (D("500"), unknown)], unmodelled=False,
    ).scenario is SolvencyScenario.UNAVAILABLE
    assert vault_solvency(
        total_assets=D("1000"), holdings=[(D("500"), healthy)], unmodelled=True,
    ).scenario is SolvencyScenario.UNAVAILABLE
    assert vault_solvency(
        total_assets=D("0"), holdings=[(D("500"), healthy)], unmodelled=False,
    ).scenario is SolvencyScenario.UNAVAILABLE
