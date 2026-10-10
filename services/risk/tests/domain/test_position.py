from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from app.contracts.models import (
    CatalogEntityType,
    CatalogEntry,
    CatalogProvenance,
    PositionInput,
    PositionMode,
)
from app.domain.position import (
    LTV_TOLERANCE,
    MAX_USD_EQUIVALENT,
    PositionValidationError,
    ValidatedPosition,
    convert_to_numeraire,
    validate_position,
)
from app.domain.snapshots import AssetInfo, MarketSnapshot, VaultSnapshot


NOW = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)
MARKET_ID = "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"
VAULT_ADDRESS = "0xD6d4b804014EF27836dBe9f8f6Bf6c71251548Ec"
# Synthetic oracle / IRM addresses; no live data is used anywhere in these tests.
ORACLE_ADDRESS = "0x000000000000000000000000000000000000beef"
IRM_ADDRESS = "0x000000000000000000000000000000000000cafe"


def asset(
    symbol: str, address: str, decimals: int, price: str | None
) -> AssetInfo:
    return AssetInfo(
        address=address,
        symbol=symbol,
        decimals=decimals,
        price_usd=None if price is None else Decimal(price),
    )


def weth(price: str | None = "2000") -> AssetInfo:
    return asset("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18, price)


def usdc(price: str | None = "1") -> AssetInfo:
    return asset("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, price)


def eurcv(price: str | None = "1.08") -> AssetInfo:
    return asset("EURCV", "0x5F7827FDeb7c20b443265Fc2F40845B715385Ff2", 18, price)


def market_entry(**overrides) -> CatalogEntry:
    values = {
        "entity_id": "weth-usdc-860",
        "chain_id": 1,
        "entity_type": CatalogEntityType.MORPHO_MARKET,
        "address_or_market_id": MARKET_ID,
        "name": "WETH collateral / USDC debt",
        "description": "Morpho market with WETH collateral, USDC debt, and 86% LLTV.",
        "provenance": CatalogProvenance.REPRESENTATIVE_PROXY,
        "provenance_label": "Representative stablecoin borrowing market",
        "supported": True,
        "supported_modes": (PositionMode.LENDER, PositionMode.BORROWER),
        "lltv": Decimal("0.86"),
    }
    values.update(overrides)
    return CatalogEntry(**values)


def vault_entry(**overrides) -> CatalogEntry:
    values = {
        "entity_id": "belem-rwa-eurcv",
        "chain_id": 1,
        "entity_type": CatalogEntityType.MORPHO_VAULT_V2,
        "address_or_market_id": VAULT_ADDRESS,
        "name": "Belem RWA EURCV",
        "description": "Belem-named Morpho Vault V2 for EURCV lending.",
        "provenance": CatalogProvenance.VERIFIED_BELEM,
        "provenance_label": "Verified Belem-named, listed Morpho vault",
        "supported": True,
        "supported_modes": (PositionMode.LENDER,),
        "lltv": None,
    }
    values.update(overrides)
    return CatalogEntry(**values)


def market_snapshot(
    *,
    loan_asset: AssetInfo | None = None,
    collateral_asset: AssetInfo | None = None,
    **overrides,
) -> MarketSnapshot:
    values = {
        "chain_id": 1,
        "market_id": MARKET_ID,
        "loan_asset": loan_asset or usdc(),
        "collateral_asset": collateral_asset or weth(),
        "oracle_address": ORACLE_ADDRESS,
        "irm_address": IRM_ADDRESS,
        "lltv": Decimal("0.860000000000000000"),
        "total_supply_assets": Decimal("10000000.000000"),
        "total_borrow_assets": Decimal("8000000.000000"),
        "liquidity_assets": Decimal("2000000.000000"),
        "utilization": Decimal("0.8"),
        "total_supply_usd": Decimal("10000000"),
        "total_borrow_usd": Decimal("8000000"),
        "liquidity_usd": Decimal("2000000"),
        "listed": True,
        "last_accrual_timestamp": 1_791_000_000,
        "sources": ("https://morpho.test/market",),
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def vault_snapshot(**overrides) -> VaultSnapshot:
    values = {
        "chain_id": 1,
        "address": VAULT_ADDRESS,
        "version": "v2",
        "name": "Belem RWA EURCV",
        "symbol": "bEURCV",
        "asset": eurcv(),
        "total_assets": Decimal("5000000.000000000000000000"),
        "liquidity_assets": Decimal("1250000.000000000000000000"),
        "idle_assets": Decimal("250000.000000000000000000"),
        "share_price": Decimal("1.02"),
        "total_assets_usd": Decimal("5400000"),
        "liquidity_usd": Decimal("1350000"),
        "listed": True,
        "sources": ("https://morpho.test/vault",),
    }
    values.update(overrides)
    return VaultSnapshot(**values)


def lender_input(
    amount: str = "1000",
    *,
    entity_id: str = "weth-usdc-860",
    numeraire: str = "USDC",
) -> PositionInput:
    return PositionInput(
        mode=PositionMode.LENDER,
        entity_id=entity_id,
        amount=Decimal(amount),
        numeraire=numeraire,
    )


def borrower_input(
    *,
    collateral: str = "2.5",
    debt: str = "3000",
    ltv: str = "0.60",
    amount: str = "3000",
    entity_id: str = "weth-usdc-860",
    numeraire: str = "USDC",
) -> PositionInput:
    return PositionInput(
        mode=PositionMode.BORROWER,
        entity_id=entity_id,
        amount=Decimal(amount),
        collateral_amount=Decimal(collateral),
        debt_amount=Decimal(debt),
        ltv=Decimal(ltv),
        numeraire=numeraire,
    )


def validate(position: PositionInput, entry=None, market=None, **kwargs) -> ValidatedPosition:
    if entry is None:
        entry = market_entry()
    if market is None and entry.entity_type is CatalogEntityType.MORPHO_MARKET:
        market = market_snapshot()
    return validate_position(position, entry, market, now=NOW, **kwargs)


# Happy paths


def test_lender_on_market_is_validated_with_market_lltv():
    validated = validate(lender_input("1000"))

    assert validated.mode is PositionMode.LENDER
    assert validated.entity_id == "weth-usdc-860"
    assert validated.numeraire == "USDC"
    assert validated.amount == Decimal("1000")
    assert validated.amount.as_tuple().exponent == -6  # quantized to USDC decimals
    assert validated.collateral_amount is None
    assert validated.debt_amount is None
    assert validated.ltv is None
    assert validated.collateral_value is None
    assert validated.debt_value is None
    assert validated.lltv == Decimal("0.86")
    assert validated.validated_at == NOW
    assert validated.warnings == ()


def test_borrower_values_are_derived_from_snapshot_prices_in_the_numeraire():
    validated = validate(borrower_input(collateral="2.5", debt="3000", ltv="0.60"))

    assert validated.mode is PositionMode.BORROWER
    assert validated.collateral_amount == Decimal("2.5")
    assert validated.collateral_amount.as_tuple().exponent == -18
    assert validated.debt_amount == Decimal("3000")
    assert validated.debt_amount.as_tuple().exponent == -6
    assert validated.collateral_value == Decimal("5000")
    assert validated.collateral_value.as_tuple().exponent == -18
    assert validated.debt_value == Decimal("3000")
    assert validated.debt_value.as_tuple().exponent == -18
    assert validated.ltv == Decimal("0.60")
    assert validated.lltv == Decimal("0.86")
    assert validated.amount == Decimal("3000")
    assert validated.warnings == ()


def test_borrower_values_in_usd_numeraire_use_both_prices():
    validated = validate(borrower_input(numeraire="USD", amount="3000"))

    assert validated.numeraire == "USD"
    assert validated.collateral_value == Decimal("5000")
    assert validated.debt_value == Decimal("3000")


def test_borrower_values_in_collateral_numeraire_convert_the_debt():
    validated = validate(borrower_input(numeraire="WETH", amount="1.5"))

    assert validated.collateral_value == Decimal("2.5")
    assert validated.debt_value == Decimal("1.5")


def test_numeraire_is_matched_case_insensitively_and_canonicalized():
    validated = validate(lender_input(numeraire="usdc"))

    assert validated.numeraire == "USDC"


def test_vault_lender_without_snapshot_has_no_lltv_and_18_place_amount():
    validated = validate(
        lender_input("1000.5", entity_id="belem-rwa-eurcv", numeraire="EURCV"),
        vault_entry(),
    )

    assert validated.lltv is None
    assert validated.amount == Decimal("1000.5")
    assert validated.amount.as_tuple().exponent == -18
    assert validated.collateral_value is None


def test_vault_lender_with_snapshot_checks_numeraire_against_the_vault_asset():
    validated = validate(
        lender_input("1000", entity_id="belem-rwa-eurcv", numeraire="usd"),
        vault_entry(),
        vault=vault_snapshot(),
    )
    assert validated.numeraire == "USD"

    with pytest.raises(PositionValidationError, match="numeraire"):
        validate(
            lender_input("1000", entity_id="belem-rwa-eurcv", numeraire="USDC"),
            vault_entry(),
            vault=vault_snapshot(),
        )


def test_validated_position_is_immutable():
    validated = validate(lender_input())

    with pytest.raises(AttributeError):
        validated.amount = Decimal("1")  # type: ignore[misc]


# Amount bounds (Review Focus)


@pytest.mark.parametrize(
    "amount",
    [
        "0",
        "-1",
        "-0.000001",
        "1e3",  # exponent notation is accepted by Pydantic but rejected here
        "1E+15",
        "1000000000000000.000001",  # above 1e15
        "2000000000000000",
    ],
)
def test_lender_amount_bounds_are_rejected(amount):
    with pytest.raises(PositionValidationError, match="amount"):
        validate(lender_input(amount))


def test_lender_amount_exactly_at_cap_is_accepted():
    validated = validate(lender_input(str(MAX_USD_EQUIVALENT)))

    assert validated.amount == MAX_USD_EQUIVALENT


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_amounts_are_rejected(value):
    position = PositionInput.model_construct(
        mode=PositionMode.LENDER,
        entity_id="weth-usdc-860",
        amount=Decimal(value),
        numeraire="USDC",
    )

    with pytest.raises(PositionValidationError, match="amount"):
        validate(position)


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_non_finite_borrower_fields_are_rejected(value):
    position = PositionInput.model_construct(
        mode=PositionMode.BORROWER,
        entity_id="weth-usdc-860",
        amount=Decimal("3000"),
        collateral_amount=Decimal(value),
        debt_amount=Decimal("3000"),
        ltv=Decimal("0.60"),
        numeraire="USDC",
    )

    with pytest.raises(PositionValidationError, match="collateral_amount"):
        validate(position)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collateral", "0"),
        ("collateral", "-2.5"),
        ("collateral", "25E+1"),
        ("debt", "0"),
        ("debt", "-3000"),
        ("debt", "3e3"),
        ("amount", "0"),
        ("amount", "-3000"),
        ("amount", "3E+3"),
        ("ltv", "0"),
        ("ltv", "-0.5"),
    ],
)
def test_borrower_field_bounds_are_rejected(field, value):
    with pytest.raises(PositionValidationError):
        validate(borrower_input(**{field: value}))


def test_usd_equivalent_above_cap_is_rejected_even_when_token_amount_is_small():
    # 1e12 WETH at 2000 USD is 2e15 USD-equivalent.
    with pytest.raises(PositionValidationError, match="1000000000000000"):
        validate(
            borrower_input(
                collateral="1000000000000",
                debt="1200000000000000",
                ltv="0.60",
                amount="1200000000000000",
            )
        )


def test_lender_usd_equivalent_above_cap_is_rejected():
    market = market_snapshot(loan_asset=weth(), collateral_asset=usdc())
    with pytest.raises(PositionValidationError, match="USD-equivalent"):
        validate(lender_input("600000000000", numeraire="WETH"), market=market)


# Borrower LTV against LLTV (Review Focus)


def test_borrower_ltv_just_below_lltv_passes():
    validated = validate(borrower_input(collateral="1.75", debt="3000", ltv="0.859999"))

    assert validated.ltv == Decimal("0.859999")


@pytest.mark.parametrize("ltv", ["0.86", "0.860000000000000001", "0.9", "1", "1.5"])
def test_borrower_ltv_at_or_above_lltv_is_rejected(ltv):
    with pytest.raises(PositionValidationError, match="LLTV"):
        validate(borrower_input(collateral="1.75", debt="3000", ltv=ltv))


def test_derived_ltv_at_or_above_lltv_is_rejected_even_when_declared_ltv_is_below():
    # 1.7392 WETH * 2000 = 3478.4; 3000 / 3478.4 = 0.86247 >= 0.86 while the
    # declared 0.855 is within tolerance of the derived value.
    with pytest.raises(PositionValidationError, match="LLTV"):
        validate(borrower_input(collateral="1.7392", debt="3000", ltv="0.855"))


def test_declared_ltv_inconsistent_with_prices_beyond_tolerance_is_rejected():
    with pytest.raises(PositionValidationError, match="inconsistent"):
        validate(borrower_input(collateral="2.5", debt="3000", ltv="0.70"))


def test_declared_ltv_within_tolerance_is_accepted():
    declared = Decimal("0.60") + LTV_TOLERANCE - Decimal("0.0001")

    validated = validate(borrower_input(collateral="2.5", debt="3000", ltv=str(declared)))

    assert validated.ltv == declared


# Mode shape


def test_lender_request_cannot_include_debt():
    position = PositionInput.model_construct(
        mode=PositionMode.LENDER,
        entity_id="weth-usdc-860",
        amount=Decimal("1000"),
        debt_amount=Decimal("10"),
        numeraire="USDC",
    )

    with pytest.raises(PositionValidationError, match="lender"):
        validate(position)


@pytest.mark.parametrize("field", ["collateral_amount", "debt_amount", "ltv"])
def test_borrower_request_must_include_collateral_debt_and_ltv(field):
    values = {
        "mode": PositionMode.BORROWER,
        "entity_id": "weth-usdc-860",
        "amount": Decimal("3000"),
        "collateral_amount": Decimal("2.5"),
        "debt_amount": Decimal("3000"),
        "ltv": Decimal("0.60"),
        "numeraire": "USDC",
    }
    values[field] = None
    position = PositionInput.model_construct(**values)

    with pytest.raises(PositionValidationError, match=field):
        validate(position)


# Entry and snapshot consistency


def test_unsupported_mode_for_entry_is_rejected():
    position = PositionInput(
        mode=PositionMode.BORROWER,
        entity_id="belem-rwa-eurcv",
        amount=Decimal("1"),
        collateral_amount=Decimal("1"),
        debt_amount=Decimal("1"),
        ltv=Decimal("0.5"),
        numeraire="EURCV",
    )

    with pytest.raises(PositionValidationError, match="mode"):
        validate(position, vault_entry())


def test_inactive_entry_is_rejected():
    with pytest.raises(PositionValidationError, match="inactive"):
        validate(lender_input(), market_entry(supported=False))


def test_entity_id_mismatch_is_rejected():
    with pytest.raises(PositionValidationError, match="entity_id"):
        validate(lender_input(entity_id="wbtc-usdc-860"))


def test_market_entry_requires_a_market_snapshot():
    with pytest.raises(PositionValidationError, match="market"):
        validate_position(lender_input(), market_entry(), None, now=NOW)


def test_vault_entry_rejects_a_market_snapshot():
    with pytest.raises(PositionValidationError, match="market"):
        validate_position(
            lender_input(entity_id="belem-rwa-eurcv", numeraire="EURCV"),
            vault_entry(),
            market_snapshot(),
            now=NOW,
        )


def test_market_snapshot_must_match_the_entry():
    other = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
    with pytest.raises(PositionValidationError, match="market"):
        validate(lender_input(), market=market_snapshot(market_id=other))


def test_market_id_comparison_ignores_hex_case():
    validated = validate(lender_input(), market=market_snapshot(market_id=MARKET_ID.upper().replace("0X", "0x")))

    assert validated.lltv == Decimal("0.86")


def test_catalog_and_market_lltv_conflict_is_rejected():
    with pytest.raises(PositionValidationError, match="LLTV"):
        validate(lender_input(), market=market_snapshot(lltv=Decimal("0.915")))


def test_delisted_market_is_rejected():
    with pytest.raises(PositionValidationError, match="listed"):
        validate(lender_input(), market=market_snapshot(listed=False))


def test_unsupported_numeraire_is_rejected():
    with pytest.raises(PositionValidationError, match="numeraire"):
        validate(lender_input(numeraire="EUR"))


def test_lender_cannot_use_the_collateral_asset_as_numeraire():
    with pytest.raises(PositionValidationError, match="numeraire"):
        validate(lender_input(numeraire="WETH"))


def test_naive_validation_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        validate_position(
            lender_input(), market_entry(), market_snapshot(), now=NOW.replace(tzinfo=None)
        )


# Missing prices are never invented


def test_missing_collateral_price_leaves_value_none_with_a_warning():
    market = market_snapshot(collateral_asset=weth(price=None))

    validated = validate(borrower_input(), market=market)

    assert validated.collateral_value is None
    assert validated.debt_value == Decimal("3000")
    assert validated.ltv == Decimal("0.60")
    assert any("WETH" in warning and "price" in warning for warning in validated.warnings)


def test_missing_loan_price_in_usd_numeraire_leaves_debt_value_none_with_a_warning():
    market = market_snapshot(loan_asset=usdc(price=None))

    validated = validate(borrower_input(numeraire="USD"), market=market)

    assert validated.debt_value is None
    assert validated.collateral_value == Decimal("5000")
    assert any("USDC" in warning for warning in validated.warnings)


def test_zero_price_is_treated_as_unusable_not_as_a_free_asset():
    market = market_snapshot(collateral_asset=weth(price="0"))

    validated = validate(borrower_input(), market=market)

    assert validated.collateral_value is None
    assert validated.debt_value == Decimal("3000")
    assert any("WETH" in warning for warning in validated.warnings)


def test_missing_price_still_rejects_declared_ltv_at_lltv():
    market = market_snapshot(collateral_asset=weth(price=None))

    with pytest.raises(PositionValidationError, match="LLTV"):
        validate(borrower_input(ltv="0.86"), market=market)


# Quantization (Review Focus: no float drift)


def test_token_amounts_are_quantized_to_token_decimals_conservatively():
    validated = validate(
        borrower_input(
            collateral="2.123456789012345678901",  # 21 places; WETH has 18
            debt="3000.1234567",  # 7 places; USDC has 6
            ltv="0.7068",
            amount="3000.1234567",
        )
    )

    assert validated.collateral_amount == Decimal("2.123456789012345678")  # rounded down
    assert validated.debt_amount == Decimal("3000.123457")  # rounded up
    assert validated.amount == Decimal("3000.123457")


def test_large_18_decimal_values_round_without_float_drift():
    collateral = "123456789.123456789012345678"
    market = market_snapshot(collateral_asset=weth(price="1999.99"))
    with localcontext() as context:
        context.prec = 80
        expected = (Decimal(collateral) * Decimal("1999.99")).quantize(
            Decimal("1e-18"), rounding=ROUND_DOWN
        )
        debt = (expected * Decimal("0.5")).quantize(Decimal("1e-6"), rounding=ROUND_DOWN)

    validated = validate(
        borrower_input(collateral=collateral, debt=str(debt), ltv="0.5", amount=str(debt)),
        market=market,
    )

    assert validated.collateral_value == expected
    assert validated.collateral_value.as_tuple().exponent == -18
    assert validated.debt_value == debt
    assert validated.debt_value.as_tuple().exponent == -18


def test_convert_to_numeraire_returns_none_instead_of_guessing():
    assert convert_to_numeraire(Decimal("1"), weth(price=None), "USD") is None
    assert convert_to_numeraire(Decimal("1"), weth(), "EUR", (usdc(),)) is None
    assert convert_to_numeraire(Decimal("1"), weth(), "WETH") == Decimal("1")
    assert convert_to_numeraire(Decimal("1"), weth(), "USD") == Decimal("2000")
    assert convert_to_numeraire(Decimal("1"), weth(), "USDC", (usdc(),)) == Decimal("2000")
    assert convert_to_numeraire(Decimal("1"), weth(), "USDC", (usdc(price=None),)) is None
