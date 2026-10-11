"""Deterministic lender solvency model: permanent bad debt at a point in time.

A lender in a Morpho market loses only the debt the market's collateral can no
longer cover once it is liquidated. This module prices that loss from four
observable inputs, with no probabilities and no temporary illiquidity:

- `Q`: collateral (collateral-token units) of borrowers in the liquidation
  zone, i.e. whose current LTV is at least `LIQUIDATION_ZONE_SHARE` of LLTV.
  Morpho's health factor is `HF = collateral * price * LLTV / debt`, so
  `LTV = LLTV / HF` and the zone is `HF <= 1 / LIQUIDATION_ZONE_SHARE`.
- `R`: exit liquidity depth for the collateral (collateral-token units), the
  constant-product reserve implied by a Uniswap quote of `Q`.
- `P_contract`: the market's Morpho oracle price (loan per collateral).
- `P_real`: the minimum of the independent price sources available.

Decision tree (per market):

- Scenario A (systemic, bank run) when `Q > R / 2`: suppliers recover
  `R_PANIC`, so their loss fraction is `1 - R_PANIC`.
- Scenario B (degraded normal) when `Q <= R / 2`:
  `debt_issued = Q * P_contract * LLTV`,
  `recovered_value = Q * P_real * (R / (R + Q))^2`,
  `bad_debt = max(0, debt_issued - recovered_value)`, and the supplier loss
  fraction is `bad_debt / total_supply`.

A holder of `s` units of supply loses `s * loss_fraction`, so a direct lender
loses `bad_debt * W_pool` with `W_pool = X / total_supply`. A vault's loss in a
market is its supply there times that market's loss fraction (in B, its share
of market supply times the bad debt); the vault's losses are summed and the
position takes `W_pool = X / vault_total_assets` of them.

Never fabricated: a missing input makes the result `UNAVAILABLE` with the
missing input named, except where the decision tree cannot depend on it (an
empty liquidation zone is healthy whatever the depth or prices). Debt that the
borrower list does not cover (a truncated list, borrowers without a health
factor) is priced at the worst case a healthy borrower can be in, exactly at
LLTV, and the zone is then flagged as a bound. All arithmetic is `Decimal`.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum


ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)
PRECISION = 80
# Assumption, not data: share of collateral value suppliers recover in a systemic collapse.
R_PANIC = Decimal("0.30")
# A borrower is in the liquidation zone when its LTV is at least this share of LLTV.
LIQUIDATION_ZONE_SHARE = Decimal("0.95")


class SolvencyScenario(StrEnum):
    A_SYSTEMIC = "A_systemic"
    B_DEGRADED = "B_degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LiquidationZone:
    collateral: Decimal
    borrowers_in_zone: int
    # Debt with no rated borrower behind it, priced at LLTV into `collateral`.
    uncovered_debt: Decimal
    # True when `collateral` includes the worst-case bound for uncovered debt.
    bound: bool


def _decimal(name: str, value: Decimal | None) -> Decimal | None:
    if value is not None and not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, not {type(value).__name__}")
    if value is not None and not value.is_finite():
        return None
    return value


def liquidation_zone(
    borrowers: Iterable[tuple[Decimal, Decimal, Decimal | None]],
    *,
    lltv: Decimal,
    complete: bool,
    total_borrow: Decimal | None,
    p_contract: Decimal | None,
) -> LiquidationZone | None:
    """`Q` from `(collateral, debt, health_factor)` rows; None when it cannot be bounded."""
    if _decimal("lltv", lltv) is None or lltv <= ZERO:
        return None
    total_borrow = _decimal("total_borrow", total_borrow)
    p_contract = _decimal("p_contract", p_contract)
    with localcontext() as context:
        context.prec = PRECISION
        threshold = ONE / LIQUIDATION_ZONE_SHARE
        collateral = ZERO
        covered = ZERO
        in_zone = 0
        unrated = 0
        for row_collateral, debt, health_factor in borrowers:
            _decimal("collateral", row_collateral)
            _decimal("debt", debt)
            if debt <= ZERO:
                continue
            if health_factor is None or not health_factor.is_finite() or health_factor < ZERO:
                unrated += 1
                continue
            covered += debt
            if health_factor <= threshold:
                collateral += row_collateral
                in_zone += 1
        uncovered = ZERO
        bound = not complete or unrated > 0
        if bound:
            if total_borrow is None or p_contract is None or p_contract <= ZERO:
                return None
            uncovered = max(ZERO, total_borrow - covered)
            collateral += uncovered / (p_contract * lltv)
    return LiquidationZone(
        collateral=collateral, borrowers_in_zone=in_zone, uncovered_debt=uncovered, bound=bound
    )


@dataclass(frozen=True, slots=True)
class MarketSolvency:
    scenario: SolvencyScenario
    q: Decimal | None
    r: Decimal | None
    lltv: Decimal | None
    p_contract: Decimal | None
    p_real: Decimal | None
    r_panic: Decimal
    # Q / (R / 2): above 1 is scenario A.
    q_over_half_r: Decimal | None
    debt_issued: Decimal | None
    recovered_value: Decimal | None
    slippage_factor: Decimal | None
    bad_debt: Decimal | None
    # Share of each unit of supply lost; None when unavailable.
    supplier_loss_fraction: Decimal | None
    missing: tuple[str, ...]

    @property
    def available(self) -> bool:
        return self.scenario is not SolvencyScenario.UNAVAILABLE


def market_solvency(
    *,
    q: Decimal | None,
    r: Decimal | None,
    lltv: Decimal | None,
    p_contract: Decimal | None,
    p_real: Decimal | None,
    total_supply: Decimal | None,
    r_panic: Decimal = R_PANIC,
) -> MarketSolvency:
    q = _decimal("q", q)
    r = _decimal("r", r)
    lltv = _decimal("lltv", lltv)
    p_contract = _decimal("p_contract", p_contract)
    p_real = _decimal("p_real", p_real)
    total_supply = _decimal("total_supply", total_supply)
    _decimal("r_panic", r_panic)
    if not ZERO <= r_panic <= ONE:
        raise ValueError("r_panic must be in [0, 1]")

    def result(scenario: SolvencyScenario, missing: Sequence[str] = (), **values) -> MarketSolvency:
        fields = {
            "q_over_half_r": None,
            "debt_issued": None,
            "recovered_value": None,
            "slippage_factor": None,
            "bad_debt": None,
            "supplier_loss_fraction": None,
        }
        fields.update(values)
        return MarketSolvency(
            scenario=scenario, q=q, r=r, lltv=lltv, p_contract=p_contract, p_real=p_real,
            r_panic=r_panic, missing=tuple(missing), **fields,
        )

    if q is None or q < ZERO:
        return result(SolvencyScenario.UNAVAILABLE, ["q"])
    if q == ZERO:
        # Nobody is near liquidation: no debt can go bad, whatever the depth or prices.
        return result(
            SolvencyScenario.B_DEGRADED,
            q_over_half_r=ZERO if r is not None and r > ZERO else None,
            debt_issued=ZERO, recovered_value=ZERO, bad_debt=ZERO, supplier_loss_fraction=ZERO,
        )
    if r is None or r < ZERO:
        return result(SolvencyScenario.UNAVAILABLE, ["r"])
    with localcontext() as context:
        context.prec = PRECISION
        ratio = q / (r / TWO) if r > ZERO else None
        if r == ZERO or q > r / TWO:
            return result(
                SolvencyScenario.A_SYSTEMIC, q_over_half_r=ratio, supplier_loss_fraction=ONE - r_panic
            )
        missing = [
            name
            for name, value in (("lltv", lltv), ("p_contract", p_contract), ("p_real", p_real))
            if value is None or value < ZERO
        ]
        if missing:
            return result(SolvencyScenario.UNAVAILABLE, missing, q_over_half_r=ratio)
        assert lltv is not None and p_contract is not None and p_real is not None
        debt_issued = q * p_contract * lltv
        factor = r / (r + q)
        slippage = factor * factor
        recovered = q * p_real * slippage
        bad_debt = max(ZERO, debt_issued - recovered)
        values = {
            "q_over_half_r": ratio,
            "debt_issued": debt_issued,
            "recovered_value": recovered,
            "slippage_factor": slippage,
            "bad_debt": bad_debt,
        }
        if bad_debt == ZERO:
            return result(SolvencyScenario.B_DEGRADED, supplier_loss_fraction=ZERO, **values)
        if total_supply is None or total_supply <= ZERO:
            return result(SolvencyScenario.UNAVAILABLE, ["total_supply"], **values)
        fraction = min(ONE, bad_debt / total_supply)
        return result(SolvencyScenario.B_DEGRADED, supplier_loss_fraction=fraction, **values)


@dataclass(frozen=True, slots=True)
class VaultSolvency:
    scenario: SolvencyScenario
    total_assets: Decimal | None
    # Vault-level loss in vault-asset units: sum of supply_m * loss_fraction_m.
    bad_debt: Decimal | None
    # bad_debt / total_assets: the share of each vault unit lost.
    loss_fraction: Decimal | None


def vault_solvency(
    *,
    total_assets: Decimal | None,
    holdings: Iterable[tuple[Decimal, MarketSolvency]],
    unmodelled: bool,
) -> VaultSolvency:
    """Aggregate per-market results by the vault's supply in each market."""
    total_assets = _decimal("total_assets", total_assets)
    holdings = tuple(holdings)
    unavailable = VaultSolvency(SolvencyScenario.UNAVAILABLE, total_assets, None, None)
    if unmodelled or total_assets is None or total_assets <= ZERO:
        return unavailable
    with localcontext() as context:
        context.prec = PRECISION
        loss = ZERO
        systemic = False
        for supply, market in holdings:
            _decimal("supply", supply)
            if supply <= ZERO:
                continue
            if market.supplier_loss_fraction is None:
                return unavailable
            systemic = systemic or market.scenario is SolvencyScenario.A_SYSTEMIC
            loss += supply * market.supplier_loss_fraction
        return VaultSolvency(
            scenario=SolvencyScenario.A_SYSTEMIC if systemic else SolvencyScenario.B_DEGRADED,
            total_assets=total_assets,
            bad_debt=loss,
            loss_fraction=min(ONE, loss / total_assets),
        )


@dataclass(frozen=True, slots=True)
class LenderSolvency:
    scenario: SolvencyScenario
    x: Decimal
    w_pool: Decimal | None
    # In the position numeraire: X * loss_fraction.
    expected_loss: Decimal | None

    @property
    def vulnerable(self) -> bool | None:
        """True on a vulnerability alert (expected loss above zero), None when unknown."""
        return None if self.expected_loss is None else self.expected_loss > ZERO


def lender_solvency(
    *,
    x: Decimal,
    x_in_pool_units: Decimal | None,
    pool_total: Decimal | None,
    loss_fraction: Decimal | None,
    scenario: SolvencyScenario,
) -> LenderSolvency:
    """The position's share of the pool (`W_pool`) and its expected loss.

    `x` is in the position numeraire and `x_in_pool_units` is the same amount in
    the pool's asset (None when it cannot be converted); the expected loss is
    `x * loss_fraction`, which equals `bad_debt * W_pool` in pool units.
    """
    _decimal("x", x)
    x_in_pool_units = _decimal("x_in_pool_units", x_in_pool_units)
    pool_total = _decimal("pool_total", pool_total)
    loss_fraction = _decimal("loss_fraction", loss_fraction)
    with localcontext() as context:
        context.prec = PRECISION
        w_pool = (
            x_in_pool_units / pool_total
            if x_in_pool_units is not None and pool_total is not None and pool_total > ZERO
            else None
        )
        if scenario is SolvencyScenario.UNAVAILABLE or loss_fraction is None:
            return LenderSolvency(SolvencyScenario.UNAVAILABLE, x, w_pool, None)
        return LenderSolvency(scenario, x, w_pool, x * loss_fraction)
