from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.analysis.exit_cost import (
    BorrowerExitBreakdown,
    ExitCostService,
    ExitCostUnavailableError,
)
from app.contracts.models import ExitEstimate, FreshnessStatus, PositionMode
from app.domain.exit import (
    MAX_QUOTE_BLOCK_LAG,
    NUMERAIRE_PLACES,
    QUOTE_MAX_AGE,
    QUOTE_TTL,
    ExitQuote,
    ExitRoute,
    GasAssumptions,
    QuoteRequest,
)
from app.domain.snapshots import AssetInfo, DataEnvelope, MarketSnapshot, VaultSnapshot


NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
QUOTED_AT = NOW - timedelta(seconds=5)
SNAPSHOT_BLOCK = 1_000
WETH = AssetInfo(
    address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    symbol="WETH",
    decimals=18,
    price_usd=Decimal("3000"),
)
USDC = AssetInfo(
    address="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    symbol="USDC",
    decimals=6,
    price_usd=Decimal("1"),
)
WBTC = AssetInfo(
    address="0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
    symbol="WBTC",
    decimals=8,
    price_usd=Decimal("65000"),
)
GAS = GasAssumptions(gas_price_wei=Decimal("30000000000"), eth_price=Decimal("3000"))
WEI_PER_ETH = Decimal(10) ** 18


@dataclass(frozen=True)
class StubPosition:
    """Minimal object satisfying Task 6's ScenarioPosition protocol."""

    mode: PositionMode
    entity_id: str
    amount: Decimal
    numeraire: str = "USD"
    collateral_amount: Decimal | None = None
    debt_amount: Decimal | None = None
    ltv: Decimal | None = None
    collateral_value: Decimal | None = None
    debt_value: Decimal | None = None
    lltv: Decimal | None = None


def vault(
    asset: AssetInfo = WETH,
    liquidity: str = "12.5",
    *,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    block_number: int | None = SNAPSHOT_BLOCK,
) -> DataEnvelope[VaultSnapshot]:
    return DataEnvelope[VaultSnapshot](
        value=VaultSnapshot(
            chain_id=1,
            address="0x9a8bc3b04b7f3d87cfc09ba407dced575f2d61d8",
            version="v1",
            name="Fixture vault",
            symbol="fxWETH",
            asset=asset,
            total_assets=Decimal("100"),
            liquidity_assets=Decimal(liquidity),
            idle_assets=None,
            share_price=Decimal("1.01"),
            total_assets_usd=None,
            liquidity_usd=None,
            listed=True,
            sources=("fixture",),
        ),
        source="morpho:fixture",
        fetched_at=NOW - timedelta(seconds=20),
        freshness=freshness,
        block_number=block_number,
    )


def market(
    collateral: AssetInfo = WETH,
    loan: AssetInfo = USDC,
    liquidity: str = "500000",
    *,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
) -> DataEnvelope[MarketSnapshot]:
    return DataEnvelope[MarketSnapshot](
        value=MarketSnapshot(
            chain_id=1,
            market_id="0x" + "94" * 32,
            loan_asset=loan,
            collateral_asset=collateral,
            oracle_address="0x" + "01" * 20,
            irm_address="0x" + "02" * 20,
            lltv=Decimal("0.86"),
            total_supply_assets=Decimal("1000000"),
            total_borrow_assets=Decimal("500000"),
            liquidity_assets=Decimal(liquidity),
            utilization=Decimal("0.5"),
            total_supply_usd=None,
            total_borrow_usd=None,
            liquidity_usd=None,
            listed=True,
            last_accrual_timestamp=None,
            sources=("fixture",),
        ),
        source="morpho:fixture",
        fetched_at=NOW - timedelta(seconds=20),
        freshness=freshness,
        block_number=SNAPSHOT_BLOCK,
    )


def quote(
    token_in: AssetInfo = WETH,
    token_out: AssetInfo = USDC,
    *,
    amount_in: str = "10",
    amount_out: str = "29950",
    minimum_out: str = "29800.25",
    marginal_out: str = "29955",
    fee_cost: str = "15",
    impact_cost: str = "5",
    gas_estimate: int = 120_000,
    quoted_at: datetime = QUOTED_AT,
    block_number: int = SNAPSHOT_BLOCK,
) -> DataEnvelope[ExitQuote]:
    request = QuoteRequest(
        token_in=token_in,
        token_out=token_out,
        amount_in=Decimal(amount_in),
        eth_price_usd=Decimal("3000"),
    )
    value = ExitQuote(
        request=request,
        route=ExitRoute(
            path=(token_in.address, token_out.address),
            fee_tiers=(500,),
            symbols=(token_in.symbol, token_out.symbol),
        ),
        amount_in=Decimal(amount_in),
        amount_out=Decimal(amount_out),
        minimum_amount_out=Decimal(minimum_out),
        marginal_amount_out=Decimal(marginal_out),
        fee_fraction=Decimal("0.0005"),
        fee_cost_out=Decimal(fee_cost),
        price_impact=Decimal(impact_cost) / Decimal(marginal_out),
        price_impact_cost_out=Decimal(impact_cost),
        gas_estimate=gas_estimate,
        gas_price_wei=GAS.gas_price_wei,
        gas_cost_eth=Decimal(gas_estimate) * GAS.gas_price_wei / WEI_PER_ETH,
        gas_cost_out=None,
        net_amount_out=Decimal(amount_out),
        block_number=block_number,
        quoted_at=quoted_at,
        candidates=4,
        reverted=0,
        warnings=(),
    )
    return DataEnvelope[ExitQuote](
        value=value,
        source="uniswap-v3:quoter-v2",
        fetched_at=quoted_at,
        freshness=FreshnessStatus.FRESH,
        block_number=block_number,
    )


def lender(amount: str, numeraire: str = "USD") -> StubPosition:
    return StubPosition(
        mode=PositionMode.LENDER,
        entity_id="mev-capital-weth",
        amount=Decimal(amount),
        numeraire=numeraire,
    )


def borrower(
    collateral: str = "10",
    debt: str = "20000",
    collateral_value: str = "30000",
    debt_value: str = "20000",
) -> StubPosition:
    return StubPosition(
        mode=PositionMode.BORROWER,
        entity_id="weth-usdc-860",
        amount=Decimal(collateral),
        collateral_amount=Decimal(collateral),
        debt_amount=Decimal(debt),
        ltv=Decimal(debt_value) / Decimal(collateral_value),
        collateral_value=Decimal(collateral_value),
        debt_value=Decimal(debt_value),
        lltv=Decimal("0.86"),
    )


def gas_cost(units: int) -> Decimal:
    return (Decimal(units) * GAS.gas_price_wei / WEI_PER_ETH * GAS.eth_price).quantize(
        NUMERAIRE_PLACES
    )


@pytest.fixture
def service() -> ExitCostService:
    return ExitCostService(clock=lambda: NOW)


# Lender


def test_lender_exit_to_a_different_numeraire_includes_gas_and_the_swap(service):
    estimate = service.estimate_lender(lender("30000"), vault(), quote(), GAS)

    assert isinstance(estimate, ExitEstimate)
    assert estimate.withdrawable is True
    assert estimate.route == ("WETH", "USDC@0.05%")
    assert estimate.gas == gas_cost(GAS.morpho_withdraw_gas + 120_000)
    assert estimate.gas == Decimal("24.3")
    assert estimate.fees == Decimal("15")
    assert estimate.price_impact == Decimal("5")
    assert estimate.slippage == Decimal("149.75")
    assert estimate.total_cost == Decimal("194.05")
    assert estimate.total_cost == estimate.gas + estimate.fees + estimate.price_impact + (
        estimate.slippage
    )
    assert estimate.total_cost.as_tuple().exponent == -18
    assert estimate.quote_age_seconds == 5
    assert estimate.quote_block == SNAPSHOT_BLOCK
    assert estimate.quoted_at == QUOTED_AT
    assert estimate.retrieved_at == NOW
    assert estimate.source == "uniswap-v3:quoter-v2"
    assert estimate.freshness is FreshnessStatus.FRESH
    assert estimate.warnings == ()


def test_lender_exit_in_the_asset_numeraire_only_pays_withdrawal_gas(service):
    estimate = service.estimate_lender(
        lender("10", numeraire="WETH"), vault(), None, GasAssumptions(GAS.gas_price_wei, Decimal(1))
    )

    assert estimate.withdrawable is True
    assert estimate.route == ("WETH",)
    assert estimate.gas == Decimal("0.0045")
    assert estimate.fees == 0
    assert estimate.slippage == 0
    assert estimate.price_impact == 0
    assert estimate.total_cost == Decimal("0.0045")
    assert estimate.source == "morpho:fixture"
    assert estimate.quote_block == SNAPSHOT_BLOCK
    assert estimate.quoted_at == NOW - timedelta(seconds=20)


def test_lender_ignores_an_unneeded_quote_with_a_warning(service):
    estimate = service.estimate_lender(
        lender("10", numeraire="weth"), vault(), quote(), GasAssumptions(GAS.gas_price_wei, Decimal(1))
    )

    assert estimate.fees == 0 and estimate.price_impact == 0 and estimate.slippage == 0
    assert estimate.route == ("WETH",)
    assert any("no conversion" in warning for warning in estimate.warnings)


def test_lender_requires_a_quote_when_conversion_is_needed(service):
    with pytest.raises(ExitCostUnavailableError, match="quote"):
        service.estimate_lender(lender("30000"), vault(), None, GAS)


def test_lender_partial_withdrawal_is_reported_not_fabricated(service):
    estimate = service.estimate_lender(
        lender("10", numeraire="WETH"), vault(liquidity="4"), None, GasAssumptions(GAS.gas_price_wei, Decimal(1))
    )

    assert estimate.withdrawable is False
    assert estimate.total_cost == Decimal("0.0045")
    assert any("4 of 10 WETH" in warning for warning in estimate.warnings)
    assert any("6 WETH" in warning for warning in estimate.warnings)


def test_lender_market_supply_uses_loan_asset_liquidity(service):
    estimate = service.estimate_lender(
        lender("600000", numeraire="USDC"),
        market(liquidity="500000"),
        None,
        GasAssumptions(GAS.gas_price_wei, Decimal("3000")),
    )

    assert estimate.withdrawable is False
    assert estimate.route == ("USDC",)
    assert any("500000 of 600000 USDC" in warning for warning in estimate.warnings)


def test_lender_1e18_amounts_round_trip_without_drift(service):
    one_wei = Decimal("1E-18")
    liquidity = "1.000000000000000001"
    assumptions = GasAssumptions(Decimal("1"), Decimal(1))

    exact = service.estimate_lender(
        lender(liquidity, numeraire="WETH"), vault(liquidity=liquidity), None, assumptions
    )
    short = service.estimate_lender(
        lender("1.000000000000000002", numeraire="WETH"),
        vault(liquidity=liquidity),
        None,
        assumptions,
    )

    assert exact.withdrawable is True
    assert short.withdrawable is False
    assert any(f"{one_wei:f} WETH" in warning for warning in short.warnings)
    assert exact.gas == (Decimal(GAS.morpho_withdraw_gas) / WEI_PER_ETH).quantize(NUMERAIRE_PLACES)


def test_lender_expired_liquidity_snapshot_cannot_confirm_withdrawal(service):
    estimate = service.estimate_lender(
        lender("30000"), vault(freshness=FreshnessStatus.EXPIRED), quote(), GAS
    )

    assert estimate.withdrawable is False
    assert any("expired" in warning for warning in estimate.warnings)


def test_lender_quote_must_match_the_withdrawal_asset(service):
    with pytest.raises(ExitCostUnavailableError, match="WBTC"):
        service.estimate_lender(lender("30000"), vault(), quote(token_in=WBTC), GAS)


def test_unvaluable_numeraire_is_unavailable_not_zero(service):
    with pytest.raises(ExitCostUnavailableError, match="EUR"):
        service.estimate_lender(lender("30000", numeraire="EUR"), vault(), quote(), GAS)


# USD numeraire: a USD stablecoin is reported in USD directly and never swapped


def test_lender_usd_stable_in_a_usd_numeraire_needs_no_swap(service):
    estimate = service.estimate_lender(
        lender("30000", numeraire="USD"), vault(asset=USDC, liquidity="500000"), None, GAS
    )

    assert estimate.withdrawable is True
    assert estimate.route == ("USDC",)
    assert estimate.fees == 0 and estimate.slippage == 0 and estimate.price_impact == 0
    assert estimate.total_cost == gas_cost(GAS.morpho_withdraw_gas)
    assert estimate.source == "morpho:fixture"
    assert estimate.warnings == ()


def test_lender_usd_stable_ignores_a_quote_instead_of_billing_a_fabricated_swap(service):
    bogus = quote(
        token_in=USDC,
        token_out=WETH,
        amount_in="30000",
        amount_out="10",
        minimum_out="9.95",
        marginal_out="10.01",
        fee_cost="0.005",
        impact_cost="0.01",
    )

    estimate = service.estimate_lender(lender("30000"), market(liquidity="500000"), bogus, GAS)

    assert estimate.route == ("USDC",)
    assert estimate.fees == 0 and estimate.slippage == 0 and estimate.price_impact == 0
    assert estimate.total_cost == gas_cost(GAS.morpho_withdraw_gas)
    assert any("Quote ignored" in warning for warning in estimate.warnings)


def test_lender_quote_must_buy_an_asset_reported_in_the_numeraire(service):
    into_wbtc = quote(
        token_in=WETH,
        token_out=WBTC,
        amount_in="10",
        amount_out="0.46",
        minimum_out="0.4577",
        marginal_out="0.4615",
        fee_cost="0.0002",
        impact_cost="0.0015",
    )

    with pytest.raises(ExitCostUnavailableError, match="WBTC"):
        service.estimate_lender(lender("30000"), vault(), into_wbtc, GAS)


# Quote warnings


def test_quote_warnings_propagate_into_the_estimate(service):
    probe_warning = (
        "Marginal probe output was below the full quote; price impact was floored at zero."
    )
    lender_quote = quote(impact_cost="0")
    flagged_lender_quote = lender_quote.model_copy(
        update={"value": lender_quote.value.model_copy(update={"warnings": (probe_warning,)})}
    )
    borrower_quote = quote(
        amount_in="6.7", amount_out="20050", minimum_out="19949.75", marginal_out="20060", fee_cost="10", impact_cost="0"
    )
    flagged_borrower_quote = borrower_quote.model_copy(
        update={"value": borrower_quote.value.model_copy(update={"warnings": (probe_warning,)})}
    )

    lender_estimate = service.estimate_lender(lender("30000"), vault(), flagged_lender_quote, GAS)
    borrower_estimate = service.estimate_borrower(
        borrower(), market(), flagged_borrower_quote, GAS
    )

    assert probe_warning in lender_estimate.warnings
    assert probe_warning in borrower_estimate.warnings


# Quote freshness and block lag


@pytest.mark.parametrize(
    ("age_seconds", "freshness"),
    [
        (0, FreshnessStatus.FRESH),
        (14, FreshnessStatus.FRESH),
        (15, FreshnessStatus.STALE_USABLE),
        (29, FreshnessStatus.STALE_USABLE),
        (30, FreshnessStatus.EXPIRED),
        (120, FreshnessStatus.EXPIRED),
    ],
)
def test_quote_age_maps_to_freshness_thresholds(service, age_seconds, freshness):
    quoted_at = NOW - timedelta(seconds=age_seconds)

    estimate = service.estimate_lender(lender("30000"), vault(), quote(quoted_at=quoted_at), GAS)

    assert QUOTE_TTL == timedelta(seconds=15)
    assert QUOTE_MAX_AGE == timedelta(seconds=30)
    assert estimate.quote_age_seconds == age_seconds
    assert estimate.freshness is freshness
    expired_warning = any("cannot support" in warning for warning in estimate.warnings)
    assert expired_warning is (freshness is FreshnessStatus.EXPIRED)


def test_quote_from_a_materially_older_block_is_expired_with_a_warning(service):
    assert MAX_QUOTE_BLOCK_LAG == 20
    within = service.estimate_lender(
        lender("30000"), vault(), quote(block_number=SNAPSHOT_BLOCK - 20), GAS
    )
    behind = service.estimate_lender(
        lender("30000"), vault(), quote(block_number=SNAPSHOT_BLOCK - 21), GAS
    )

    assert within.warnings == ()
    assert within.freshness is FreshnessStatus.FRESH
    assert behind.quote_block == SNAPSHOT_BLOCK - 21
    assert behind.freshness is FreshnessStatus.EXPIRED
    assert any("21 blocks" in warning and "20" in warning for warning in behind.warnings)


def test_future_clock_never_yields_negative_age(service):
    estimate = ExitCostService(clock=lambda: QUOTED_AT - timedelta(seconds=3)).estimate_lender(
        lender("30000"), vault(), quote(), GAS
    )

    assert estimate.quote_age_seconds == 0


# Borrower


def test_borrower_exit_sells_collateral_and_includes_every_component(service):
    position = borrower()
    swap = quote(amount_in="6.7", amount_out="20050", minimum_out="19949.75", marginal_out="20060", fee_cost="10", impact_cost="4")

    estimate = service.estimate_borrower(position, market(), swap, GAS)
    breakdown = service.borrower_breakdown(position, market(), swap, GAS)

    assert isinstance(breakdown, BorrowerExitBreakdown)
    assert breakdown.swap_required is True
    assert breakdown.debt_repayment == Decimal("20000")
    assert breakdown.collateral_sold == Decimal("6.7")
    assert breakdown.collateral_sold_value == Decimal("20100")
    assert breakdown.swap_proceeds == Decimal("20050")
    assert breakdown.leftover_loan_asset == Decimal("50")
    assert breakdown.residual_collateral == Decimal("3.3")
    assert breakdown.residual_collateral_value == Decimal("9900")
    assert breakdown.residual_debt == 0
    assert breakdown.residual_exposure_cost == 0

    assert estimate.withdrawable is True
    assert estimate.route == ("WETH", "USDC@0.05%")
    assert estimate.gas == gas_cost(
        GAS.morpho_repay_gas + GAS.morpho_withdraw_collateral_gas + 120_000
    )
    assert estimate.gas == Decimal("31.5")
    assert estimate.fees == Decimal("10")
    assert estimate.price_impact == Decimal("4")
    assert estimate.slippage == Decimal("100.25")
    assert estimate.total_cost == Decimal("145.75")
    assert estimate.total_cost == (
        estimate.gas + estimate.fees + estimate.price_impact + estimate.slippage
    )
    assert estimate.quote_age_seconds == 5
    assert estimate.freshness is FreshnessStatus.FRESH


def test_borrower_residual_debt_is_a_cost_and_blocks_the_exit(service):
    swap = quote(amount_in="6.7", amount_out="19000", minimum_out="18905", marginal_out="20060", fee_cost="10", impact_cost="4")

    estimate = service.estimate_borrower(borrower(), market(), swap, GAS)
    breakdown = service.borrower_breakdown(borrower(), market(), swap, GAS)

    assert breakdown.residual_debt == Decimal("1000")
    assert breakdown.residual_exposure_cost == Decimal("1000")
    assert breakdown.leftover_loan_asset == 0
    assert estimate.withdrawable is False
    assert estimate.total_cost == (
        estimate.gas + estimate.fees + estimate.price_impact + estimate.slippage + Decimal("1000")
    )
    assert any("1000 USDC" in warning for warning in estimate.warnings)


def test_borrower_with_matching_assets_repays_from_existing_assets(service):
    position = StubPosition(
        mode=PositionMode.BORROWER,
        entity_id="synthetic",
        amount=Decimal("30000"),
        collateral_amount=Decimal("30000"),
        debt_amount=Decimal("20000"),
        ltv=Decimal("20000") / Decimal("30000"),
        collateral_value=Decimal("30000"),
        debt_value=Decimal("20000"),
        lltv=Decimal("0.86"),
    )

    estimate = service.estimate_borrower(position, market(collateral=USDC, loan=USDC), None, GAS)
    breakdown = service.borrower_breakdown(position, market(collateral=USDC, loan=USDC), None, GAS)

    assert breakdown.swap_required is False
    assert breakdown.collateral_sold == Decimal("20000")
    assert breakdown.residual_collateral == Decimal("10000")
    assert estimate.withdrawable is True
    assert estimate.route == ("USDC",)
    assert estimate.fees == 0 and estimate.price_impact == 0 and estimate.slippage == 0
    assert estimate.gas == gas_cost(GAS.morpho_repay_gas + GAS.morpho_withdraw_collateral_gas)
    assert estimate.total_cost == estimate.gas


def test_borrower_requires_a_quote_when_collateral_must_be_swapped(service):
    with pytest.raises(ExitCostUnavailableError, match="quote"):
        service.estimate_borrower(borrower(), market(), None, GAS)


def test_borrower_quote_must_sell_collateral_for_the_loan_asset(service):
    with pytest.raises(ExitCostUnavailableError, match="collateral"):
        service.estimate_borrower(borrower(), market(), quote(token_in=WBTC), GAS)
    with pytest.raises(ExitCostUnavailableError, match="loan asset"):
        service.estimate_borrower(borrower(), market(), quote(token_out=WBTC), GAS)


def test_borrower_stale_snapshot_carries_a_warning_like_the_lender(service):
    swap = quote(amount_in="6.7", amount_out="20050", minimum_out="19949.75", marginal_out="20060", fee_cost="10", impact_cost="4")

    estimate = service.estimate_borrower(
        borrower(), market(freshness=FreshnessStatus.STALE_USABLE), swap, GAS
    )

    assert estimate.withdrawable is True
    assert any("stale" in warning for warning in estimate.warnings)


def test_borrower_cannot_sell_more_collateral_than_held(service):
    swap = quote(amount_in="11", amount_out="32000", minimum_out="31840", marginal_out="32010", fee_cost="10", impact_cost="4")

    estimate = service.estimate_borrower(borrower(), market(), swap, GAS)

    assert estimate.withdrawable is False
    assert any("11 WETH" in warning and "10 WETH" in warning for warning in estimate.warnings)


def test_borrower_rejects_lender_positions_and_vice_versa(service):
    with pytest.raises(ExitCostUnavailableError, match="borrower"):
        service.estimate_borrower(lender("1"), market(), quote(), GAS)
    with pytest.raises(ExitCostUnavailableError, match="lender"):
        service.estimate_lender(borrower(), vault(), quote(), GAS)


def test_borrower_1e18_collateral_units_stay_exact(service):
    position = borrower(collateral="10.000000000000000001", collateral_value="30000.000000000000003")
    swap = quote(amount_in="6.700000000000000001", amount_out="20050", minimum_out="19949.75", marginal_out="20060", fee_cost="10", impact_cost="4")

    breakdown = service.borrower_breakdown(position, market(), swap, GAS)

    assert breakdown.residual_collateral == Decimal("3.3")
    assert breakdown.collateral_sold_value == Decimal("20100.000000000000003")
    assert breakdown.residual_collateral_value == Decimal("9900")
