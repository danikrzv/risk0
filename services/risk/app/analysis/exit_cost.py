"""Exit-cost estimation in the position numeraire (spec § 12).

Lender exit: validate the immediately withdrawable liquidity in the vault or
market snapshot, add Morpho withdrawal gas, and add Uniswap fees, slippage, and
price impact only when the withdrawal asset differs from the numeraire. A partial
or unconfirmable withdrawal is reported (`withdrawable=False` plus a warning) and
never counted as a cost or papered over.

Borrower exit: repay the debt, selling collateral for the loan asset through the
quoted route unless collateral and loan asset coincide, then withdraw the
remaining collateral. The cost is swap fee + slippage buffer + price impact +
Morpho and swap gas + residual exposure, where residual exposure is the value of
debt the quoted swap cannot repay (zero when the exit closes the position). Debt
repayment itself is reported in `BorrowerExitBreakdown`, not counted as a loss:
it is matched by collateral the position already holds.

Every `ExitEstimate` component is a numeraire amount quantized to 18 decimal
places (`price_impact` included), so `total_cost` is their exact sum. Prices come
from the snapshot (lender: asset `price_usd` for a USD numeraire, 1 when the
numeraire is the asset) or from the validated position (borrower: value / amount).
Anything that cannot be valued raises `ExitCostUnavailableError` instead of
becoming zero.

Swap rule (lender): an asset is reported in the numeraire *without* a swap when it
is the numeraire itself, or when the numeraire is USD and the asset is one of the
listed USD stablecoins (`USD_STABLE_SYMBOLS`), valued at its snapshot `price_usd`.
Every other asset must be swapped, and the quote must both sell the withdrawal
asset and buy an asset that is itself reported in the numeraire; a quote into any
other token is rejected rather than costed.

Quote timing: a quote at or beyond `QUOTE_MAX_AGE`, or more than
`MAX_QUOTE_BLOCK_LAG` blocks away from the snapshot, is reported `EXPIRED`. The
quote's own warnings (probe rounding, gross-output ranking) are carried into the
estimate.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, localcontext

from app.contracts.models import ExitEstimate, FreshnessStatus, PositionMode
from app.domain.exit import (
    MAX_QUOTE_BLOCK_LAG,
    NUMERAIRE_PLACES,
    QUOTE_MAX_AGE,
    ExitQuote,
    GasAssumptions,
    quote_freshness,
)
from app.domain.scenario import ScenarioPosition
from app.domain.snapshots import AssetInfo, DataEnvelope, MarketSnapshot, VaultSnapshot


CALCULATION_PRECISION = 60
ZERO = Decimal(0)
ONE = Decimal(1)
USD_NUMERAIRES = frozenset({"usd"})
# USD-pegged stablecoins that are reported in a USD numeraire at their snapshot
# `price_usd` without a swap. Deliberately a short, explicit allowlist: anything
# else (WETH, WBTC, ...) must be swapped into one of these through a quote.
USD_STABLE_SYMBOLS = frozenset({"usdc", "usdt", "dai", "usds", "pyusd"})


class ExitCostUnavailableError(ValueError):
    """The exit cost cannot be estimated honestly from the given inputs."""


@dataclass(frozen=True, slots=True)
class BorrowerExitBreakdown:
    """Accounting of a borrower exit; token amounts unless suffixed `_value`/`_cost`."""

    swap_required: bool
    debt_repayment: Decimal
    collateral_sold: Decimal
    collateral_sold_value: Decimal
    swap_proceeds: Decimal
    leftover_loan_asset: Decimal
    residual_collateral: Decimal
    residual_collateral_value: Decimal
    residual_debt: Decimal
    residual_exposure_cost: Decimal


@dataclass(frozen=True, slots=True)
class _SwapCosts:
    fees: Decimal
    slippage: Decimal
    price_impact: Decimal
    gas_units: int


@dataclass(frozen=True, slots=True)
class _QuoteMeta:
    quoted_at: datetime
    quote_block: int | None
    source: str
    freshness: FreshnessStatus
    age_seconds: int
    warnings: tuple[str, ...]


class ExitCostService:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock

    # Lender

    def estimate_lender(
        self,
        position: ScenarioPosition,
        snapshot: DataEnvelope[VaultSnapshot] | DataEnvelope[MarketSnapshot],
        quote: DataEnvelope[ExitQuote] | None,
        gas: GasAssumptions,
    ) -> ExitEstimate:
        if position.mode != PositionMode.LENDER:
            raise ExitCostUnavailableError(
                f"estimate_lender requires a lender position; got {position.mode.value}"
            )
        now = _aware(self._clock())
        asset = _withdrawal_asset(snapshot.value)
        liquidity = snapshot.value.liquidity_assets
        numeraire = position.numeraire
        warnings: list[str] = []

        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            price = _price_in_numeraire(asset, numeraire)
            requested = _token_floor(position.amount / price, asset.decimals)
            withdrawable_amount = min(requested, liquidity)
            withdrawable = liquidity >= requested
            if not withdrawable:
                shortfall = requested - withdrawable_amount
                warnings.append(
                    f"Only {_fmt(withdrawable_amount)} of {_fmt(requested)} {asset.symbol} "
                    f"can be withdrawn immediately; {_fmt(shortfall)} {asset.symbol} is "
                    "unavailable (reported separately, not a cost)."
                )
            if snapshot.freshness is FreshnessStatus.EXPIRED:
                withdrawable = False
                warnings.append(
                    f"Liquidity snapshot from {snapshot.source} is expired; immediate "
                    "withdrawal cannot be confirmed."
                )
            elif snapshot.freshness is FreshnessStatus.STALE_USABLE:
                warnings.append(f"Liquidity snapshot from {snapshot.source} is stale.")

            swap_required = not _reported_in_numeraire(asset, numeraire)
            if swap_required:
                if quote is None:
                    raise ExitCostUnavailableError(
                        f"converting {asset.symbol} to {numeraire} requires a Uniswap quote"
                    )
                _require_token(quote.value, "token_in", asset, "the withdrawal asset")
                _require_numeraire_token(quote.value, numeraire)
                out_price = _price_in_numeraire(quote.value.request.token_out, numeraire)
                swap = _swap_costs(quote.value, out_price)
                if quote.value.amount_in != withdrawable_amount:
                    warnings.append(
                        f"Quote sells {_fmt(quote.value.amount_in)} {asset.symbol} while "
                        f"{_fmt(withdrawable_amount)} {asset.symbol} would be withdrawn; "
                        "swap costs are taken from the quote as quoted."
                    )
                route = quote.value.route.steps()
            else:
                if quote is not None:
                    warnings.append(
                        f"Quote ignored: no conversion from {asset.symbol} to {numeraire} "
                        "is required."
                    )
                swap = _SwapCosts(ZERO, ZERO, ZERO, 0)
                route = (asset.symbol,)
            gas_cost = _quantize(gas.cost(gas.morpho_withdraw_gas + swap.gas_units))
            total = gas_cost + swap.fees + swap.slippage + swap.price_impact

        meta = _quote_meta(quote if swap_required else None, snapshot, now)
        return ExitEstimate(
            withdrawable=withdrawable,
            route=route,
            slippage=swap.slippage,
            fees=swap.fees,
            gas=gas_cost,
            price_impact=swap.price_impact,
            total_cost=_quantize(total),
            quote_age_seconds=meta.age_seconds,
            quote_block=meta.quote_block,
            quoted_at=meta.quoted_at,
            source=meta.source,
            retrieved_at=now,
            freshness=meta.freshness,
            warnings=tuple(warnings) + meta.warnings,
        )

    # Borrower

    def estimate_borrower(
        self,
        position: ScenarioPosition,
        snapshot: DataEnvelope[MarketSnapshot],
        quote: DataEnvelope[ExitQuote] | None,
        gas: GasAssumptions,
    ) -> ExitEstimate:
        return self._borrower(position, snapshot, quote, gas)[0]

    def borrower_breakdown(
        self,
        position: ScenarioPosition,
        snapshot: DataEnvelope[MarketSnapshot],
        quote: DataEnvelope[ExitQuote] | None,
        gas: GasAssumptions,
    ) -> BorrowerExitBreakdown:
        return self._borrower(position, snapshot, quote, gas)[1]

    def _borrower(
        self,
        position: ScenarioPosition,
        snapshot: DataEnvelope[MarketSnapshot],
        quote: DataEnvelope[ExitQuote] | None,
        gas: GasAssumptions,
    ) -> tuple[ExitEstimate, BorrowerExitBreakdown]:
        if position.mode != PositionMode.BORROWER:
            raise ExitCostUnavailableError(
                f"estimate_borrower requires a borrower position; got {position.mode.value}"
            )
        if not isinstance(snapshot.value, MarketSnapshot):
            raise ExitCostUnavailableError("borrower exits require a market snapshot")
        now = _aware(self._clock())
        market = snapshot.value
        collateral_asset = market.collateral_asset
        loan_asset = market.loan_asset
        collateral = position.collateral_amount
        debt = position.debt_amount
        collateral_value = position.collateral_value
        debt_value = position.debt_value
        if (
            collateral is None
            or debt is None
            or collateral_value is None
            or debt_value is None
            or collateral <= ZERO
            or debt <= ZERO
        ):
            raise ExitCostUnavailableError(
                "borrower exits require positive collateral and debt amounts and values"
            )
        warnings: list[str] = []
        withdrawable = True

        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            collateral_price = collateral_value / collateral
            loan_price = debt_value / debt
            swap_required = collateral_asset.address.lower() != loan_asset.address.lower()
            if swap_required:
                if quote is None:
                    raise ExitCostUnavailableError(
                        f"repaying {loan_asset.symbol} debt from {collateral_asset.symbol} "
                        "collateral requires a Uniswap quote"
                    )
                _require_token(quote.value, "token_in", collateral_asset, "the collateral asset")
                _require_token(quote.value, "token_out", loan_asset, "the loan asset")
                sold = quote.value.amount_in
                proceeds = quote.value.amount_out
                swap = _swap_costs(quote.value, loan_price)
                route = quote.value.route.steps()
                if sold > collateral:
                    withdrawable = False
                    warnings.append(
                        f"Quote sells {_fmt(sold)} {collateral_asset.symbol} but the position "
                        f"holds {_fmt(collateral)} {collateral_asset.symbol}."
                    )
            else:
                if quote is not None:
                    warnings.append(
                        f"Quote ignored: {collateral_asset.symbol} collateral repays "
                        f"{loan_asset.symbol} debt directly."
                    )
                sold = min(collateral, debt)
                proceeds = sold
                swap = _SwapCosts(ZERO, ZERO, ZERO, 0)
                route = (loan_asset.symbol,)

            repaid = min(proceeds, debt)
            residual_debt = debt - repaid
            leftover = proceeds - repaid
            residual_collateral = max(ZERO, collateral - sold)
            residual_cost = _quantize(residual_debt * loan_price)
            if residual_debt > ZERO:
                withdrawable = False
                warnings.append(
                    f"Quoted output {_fmt(proceeds)} {loan_asset.symbol} does not cover the "
                    f"{_fmt(debt)} {loan_asset.symbol} debt; {_fmt(residual_debt)} "
                    f"{loan_asset.symbol} would remain outstanding."
                )
            if snapshot.freshness is FreshnessStatus.EXPIRED:
                withdrawable = False
                warnings.append(
                    f"Market snapshot from {snapshot.source} is expired; the exit cannot "
                    "be confirmed."
                )
            elif snapshot.freshness is FreshnessStatus.STALE_USABLE:
                warnings.append(f"Market snapshot from {snapshot.source} is stale.")
            gas_cost = _quantize(
                gas.cost(gas.morpho_repay_gas + gas.morpho_withdraw_collateral_gas + swap.gas_units)
            )
            total = gas_cost + swap.fees + swap.slippage + swap.price_impact + residual_cost
            breakdown = BorrowerExitBreakdown(
                swap_required=swap_required,
                debt_repayment=_quantize(repaid * loan_price),
                collateral_sold=sold,
                collateral_sold_value=_quantize(sold * collateral_price),
                swap_proceeds=proceeds,
                leftover_loan_asset=leftover,
                residual_collateral=residual_collateral,
                residual_collateral_value=_quantize(residual_collateral * collateral_price),
                residual_debt=residual_debt,
                residual_exposure_cost=residual_cost,
            )

        meta = _quote_meta(quote if swap_required else None, snapshot, now)
        estimate = ExitEstimate(
            withdrawable=withdrawable,
            route=route,
            slippage=swap.slippage,
            fees=swap.fees,
            gas=gas_cost,
            price_impact=swap.price_impact,
            total_cost=_quantize(total),
            quote_age_seconds=meta.age_seconds,
            quote_block=meta.quote_block,
            quoted_at=meta.quoted_at,
            source=meta.source,
            retrieved_at=now,
            freshness=meta.freshness,
            warnings=tuple(warnings) + meta.warnings,
        )
        return estimate, breakdown


# Helpers


def _withdrawal_asset(snapshot: VaultSnapshot | MarketSnapshot) -> AssetInfo:
    if isinstance(snapshot, VaultSnapshot):
        return snapshot.asset
    if isinstance(snapshot, MarketSnapshot):
        return snapshot.loan_asset
    raise ExitCostUnavailableError("lender exits require a vault or market snapshot")


def _price_in_numeraire(asset: AssetInfo, numeraire: str) -> Decimal:
    """Value of one token in the numeraire: 1 for the asset itself, USD price for USD."""
    if asset.symbol.casefold() == numeraire.casefold():
        return ONE
    if numeraire.casefold() in USD_NUMERAIRES and asset.price_usd is not None:
        if asset.price_usd.is_finite() and asset.price_usd > ZERO:
            return asset.price_usd
    raise ExitCostUnavailableError(f"cannot value {asset.symbol} in {numeraire}")


def _reported_in_numeraire(asset: AssetInfo, numeraire: str) -> bool:
    """Whether `asset` is reported in `numeraire` without a swap.

    True when the asset is the numeraire itself, or when the numeraire is USD and
    the asset is a listed USD stablecoin (`USD_STABLE_SYMBOLS`). Anything else has
    to be swapped into such an asset through a quote.
    """
    symbol = asset.symbol.casefold()
    key = numeraire.casefold()
    return symbol == key or (key in USD_NUMERAIRES and symbol in USD_STABLE_SYMBOLS)


def _require_token(quote: ExitQuote, side: str, asset: AssetInfo, role: str) -> None:
    token = getattr(quote.request, side)
    if token.address.lower() != asset.address.lower():
        verb = "sells" if side == "token_in" else "buys"
        raise ExitCostUnavailableError(
            f"quote {verb} {token.symbol}, not {role} {asset.symbol}"
        )


def _require_numeraire_token(quote: ExitQuote, numeraire: str) -> None:
    """The quote must buy an asset that is itself reported in the numeraire."""
    token = quote.request.token_out
    if not _reported_in_numeraire(token, numeraire):
        raise ExitCostUnavailableError(
            f"quote buys {token.symbol}, which is not reported in the {numeraire} numeraire"
        )


def _swap_costs(quote: ExitQuote, out_price: Decimal) -> _SwapCosts:
    """Fee, slippage buffer, and price impact of a quote, in the numeraire."""
    if quote.fee_cost_out is None or quote.price_impact_cost_out is None:
        raise ExitCostUnavailableError(
            "quote lacks fee or price-impact data; the exit cost cannot be estimated"
        )
    return _SwapCosts(
        fees=_quantize(quote.fee_cost_out * out_price),
        slippage=_quantize((quote.amount_out - quote.minimum_amount_out) * out_price),
        price_impact=_quantize(quote.price_impact_cost_out * out_price),
        gas_units=quote.gas_estimate,
    )


def _quote_meta(
    quote: DataEnvelope[ExitQuote] | None,
    snapshot: DataEnvelope[VaultSnapshot] | DataEnvelope[MarketSnapshot],
    now: datetime,
) -> _QuoteMeta:
    """Timing of the datum the estimate rests on: the quote, or the snapshot if no swap.

    A quote's own warnings are carried into the estimate. A quote more than
    `MAX_QUOTE_BLOCK_LAG` blocks away from the snapshot is `EXPIRED`, like one at
    or beyond `QUOTE_MAX_AGE`.
    """
    if quote is None:
        quoted_at = _aware(snapshot.fetched_at)
        return _QuoteMeta(
            quoted_at=quoted_at,
            quote_block=snapshot.block_number,
            source=snapshot.source,
            freshness=snapshot.freshness,
            age_seconds=_age_seconds(quoted_at, now),
            warnings=(),
        )
    warnings: list[str] = list(quote.value.warnings)
    quoted_at = _aware(quote.fetched_at)
    age = _age_seconds(quoted_at, now)
    freshness = quote_freshness(quoted_at, now)
    if freshness is FreshnessStatus.EXPIRED:
        warnings.append(
            f"Quote is {age} s old; quotes of {int(QUOTE_MAX_AGE.total_seconds())} s or more "
            "cannot support a final recommendation and must be refreshed."
        )
    quote_block = quote.block_number
    if quote_block is not None and snapshot.block_number is not None:
        lag = abs(snapshot.block_number - quote_block)
        if lag > MAX_QUOTE_BLOCK_LAG:
            freshness = FreshnessStatus.EXPIRED
            warnings.append(
                f"Quote block {quote_block} is {lag} blocks away from snapshot block "
                f"{snapshot.block_number} (limit {MAX_QUOTE_BLOCK_LAG}); the quote is "
                "treated as expired and must be refreshed before relying on it."
            )
    return _QuoteMeta(
        quoted_at=quoted_at,
        quote_block=quote_block,
        source=quote.source,
        freshness=freshness,
        age_seconds=age,
        warnings=tuple(warnings),
    )


def _age_seconds(quoted_at: datetime, now: datetime) -> int:
    return max(0, int((now - quoted_at).total_seconds()))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _token_floor(amount: Decimal, decimals: int) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(NUMERAIRE_PLACES)


def _fmt(value: Decimal) -> str:
    return f"{value.normalize():f}"
