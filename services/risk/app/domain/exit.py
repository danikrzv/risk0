"""Exit-quote domain types shared by the Uniswap adapter and the exit-cost service.

Quantities are `Decimal` in token units (never binary floats). A quote is only an
on-chain *simulation* (QuoterV2 `eth_call`): it is not executable and carries the
block it was simulated at so callers can detect a materially older block.

Freshness (spec § 9 and § 12): a quote is `fresh` for less than 15 seconds,
`stale_usable` between 15 and 30 seconds, and `expired` at 30 seconds or more; an
expired quote cannot support a final recommendation and must be refreshed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.contracts.models import DecimalValue, FreshnessStatus, HexIdentifier
from app.domain.snapshots import AssetInfo, SnapshotModel
from app.infrastructure.freshness import Freshness


CHAIN_ID = 1
QUOTE_TTL = timedelta(seconds=15)
QUOTE_MAX_AGE = timedelta(seconds=30)
# Plan Task 10: a quote more than 20 Ethereum blocks away from the analysis
# snapshot must be refreshed or the analysis yields UNAVAILABLE.
MAX_QUOTE_BLOCK_LAG = 20
# Numeraire values and fractions are quantized to 18 decimal places.
NUMERAIRE_PLACES = Decimal("1E-18")
DEFAULT_SLIPPAGE_TOLERANCE = Decimal("0.005")
FEE_DENOMINATOR = Decimal(1_000_000)
WEI_PER_ETH = Decimal(10) ** 18


class QuoteRequest(SnapshotModel):
    """Exact-input quote request: sell `amount_in` of `token_in` for `token_out`.

    `amount_in` is in token units and must be exactly representable in the
    token's smallest unit. `eth_price_usd` lets gas be expressed in `token_out`
    units (through `token_out.price_usd`) so routes can be ranked net of gas.
    """

    token_in: AssetInfo
    token_out: AssetInfo
    amount_in: DecimalValue
    slippage_tolerance: DecimalValue = DEFAULT_SLIPPAGE_TOLERANCE
    eth_price_usd: DecimalValue | None = None

    @model_validator(mode="after")
    def validate_amounts(self) -> Self:
        if not self.amount_in.is_finite() or self.amount_in <= 0:
            raise ValueError("amount_in must be a positive finite decimal")
        if self.amount_in.scaleb(self.token_in.decimals) != int(
            self.amount_in.scaleb(self.token_in.decimals)
        ):
            raise ValueError(
                f"amount_in must be representable with {self.token_in.decimals} decimals"
            )
        if not self.slippage_tolerance.is_finite() or not (
            Decimal(0) <= self.slippage_tolerance < Decimal(1)
        ):
            raise ValueError("slippage_tolerance must be in [0, 1)")
        if self.eth_price_usd is not None and (
            not self.eth_price_usd.is_finite() or self.eth_price_usd <= 0
        ):
            raise ValueError("eth_price_usd must be positive when provided")
        if self.token_in.address.lower() == self.token_out.address.lower():
            raise ValueError("token_in and token_out must differ")
        return self

    def amount_in_units(self) -> int:
        return int(self.amount_in.scaleb(self.token_in.decimals))

    def cache_key(self) -> str:
        """Exact request key: tokens, raw amount, tolerance, and the gas-pricing inputs."""
        return ":".join(
            (
                "quote",
                str(CHAIN_ID),
                self.token_in.address.lower(),
                self.token_out.address.lower(),
                str(self.amount_in_units()),
                f"{self.slippage_tolerance:f}",
                "-" if self.eth_price_usd is None else f"{self.eth_price_usd:f}",
                "-" if self.token_out.price_usd is None else f"{self.token_out.price_usd:f}",
            )
        )


class ExitRoute(SnapshotModel):
    """Token path through Uniswap V3 pools; `fee_tiers[i]` joins `path[i]` to `path[i+1]`."""

    path: tuple[HexIdentifier, ...] = Field(min_length=2)
    fee_tiers: tuple[int, ...] = Field(min_length=1)
    symbols: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len(self.fee_tiers) != len(self.path) - 1 or len(self.symbols) != len(self.path):
            raise ValueError("route fee tiers and symbols must match the path length")
        if any(fee < 0 or fee >= FEE_DENOMINATOR for fee in self.fee_tiers):
            raise ValueError("fee tiers are hundredths of a basis point below 1e6")
        return self

    @property
    def hops(self) -> int:
        return len(self.fee_tiers)

    def fee_fraction(self) -> Decimal:
        """Combined fee taken across all hops: 1 - prod(1 - fee_i)."""
        kept = Decimal(1)
        for fee in self.fee_tiers:
            kept *= Decimal(1) - Decimal(fee) / FEE_DENOMINATOR
        return Decimal(1) - kept

    def steps(self) -> tuple[str, ...]:
        """Human-readable path, e.g. ('WBTC', 'WETH@0.3%', 'USDC@0.05%')."""
        return (
            self.symbols[0],
            *(
                f"{symbol}@{_fee_label(fee)}"
                for symbol, fee in zip(self.symbols[1:], self.fee_tiers, strict=True)
            ),
        )


class ExitQuote(SnapshotModel):
    """Best simulated exact-input quote for a request.

    Token-unit values: `amount_in` (token_in), `amount_out`, `minimum_amount_out`,
    `marginal_amount_out`, `fee_cost_out`, `price_impact_cost_out`, `gas_cost_out`,
    and `net_amount_out` (token_out). `price_impact` is the fraction of the
    marginal (depth-free) output lost to pool depth; `fee_fraction` the combined
    pool fee. `gas_cost_out` and `net_amount_out` need an ETH price; without one,
    routes are ranked by gross output and a warning says so.
    """

    request: QuoteRequest
    route: ExitRoute
    amount_in: DecimalValue
    amount_out: DecimalValue
    minimum_amount_out: DecimalValue
    marginal_amount_out: DecimalValue | None
    fee_fraction: DecimalValue
    fee_cost_out: DecimalValue | None
    price_impact: DecimalValue | None
    price_impact_cost_out: DecimalValue | None
    gas_estimate: int = Field(ge=0)
    gas_price_wei: DecimalValue
    gas_cost_eth: DecimalValue
    gas_cost_out: DecimalValue | None
    net_amount_out: DecimalValue
    block_number: int = Field(ge=0)
    quoted_at: datetime
    candidates: int = Field(ge=1)
    reverted: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GasAssumptions:
    """Gas pricing for an exit, in the position numeraire.

    `eth_price` is the value of one ETH in the numeraire (1 for a WETH numeraire).
    Morpho interaction gas units are assumptions for Morpho Blue / MetaMorpho
    calls; the swap's gas comes from the quote.
    """

    gas_price_wei: Decimal
    eth_price: Decimal
    morpho_withdraw_gas: int = 150_000
    morpho_repay_gas: int = 120_000
    morpho_withdraw_collateral_gas: int = 110_000

    def __post_init__(self) -> None:
        if not self.gas_price_wei.is_finite() or self.gas_price_wei < 0:
            raise ValueError("gas_price_wei must be a non-negative finite decimal")
        if not self.eth_price.is_finite() or self.eth_price <= 0:
            raise ValueError("eth_price must be a positive finite decimal")
        for units in (
            self.morpho_withdraw_gas,
            self.morpho_repay_gas,
            self.morpho_withdraw_collateral_gas,
        ):
            if units < 0:
                raise ValueError("gas units must be non-negative")

    def cost(self, gas_units: int) -> Decimal:
        """Numeraire cost of `gas_units` at the assumed gas and ETH prices."""
        return Decimal(gas_units) * self.gas_price_wei / WEI_PER_ETH * self.eth_price


def quote_freshness(quoted_at: datetime, now: datetime) -> FreshnessStatus:
    return FreshnessStatus(
        Freshness.from_times(
            fetched_at=quoted_at, now=now, ttl=QUOTE_TTL, maximum_age=QUOTE_MAX_AGE
        ).value
    )


def _fee_label(fee: int) -> str:
    percent = (Decimal(fee) / FEE_DENOMINATOR * 100).normalize()
    return f"{percent:f}%"
