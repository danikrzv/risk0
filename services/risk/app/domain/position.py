"""Validate a submitted position against the curated catalog and live snapshots.

This is the domain boundary behind the Pydantic contract: `PositionInput` only
guarantees shape, so every numeric rule lives here.

Rules
- Every amount is a finite `Decimal`, written in plain notation (a positive
  exponent such as `1E+3` is rejected), strictly positive, and at most
  `MAX_USD_EQUIVALENT` both in its own unit and in USD when a price is known.
- Token amounts are quantized to the token's decimals. Claims (collateral, a
  lender deposit) round down and liabilities (debt, a borrower's submitted
  amount) round up, so rounding never makes a position look safer. Numeraire
  values are quantized to `VALUE_PLACES` (18) decimal places.
- A borrower's declared LTV must be below the market LLTV, the LTV derived from
  snapshot prices must also be below the LLTV, and the two must agree within
  `LTV_TOLERANCE` (one percentage point, absolute) to absorb price drift between
  the user's view and the snapshot.
- Missing prices are never invented: the affected numeraire value is `None` and
  a warning is recorded on the validated position for the graph to surface.

The numeraire is `USD` or the symbol of an asset the position touches (loan
asset for lenders; loan or collateral asset for borrowers; vault asset for vault
lenders). Without a vault snapshot the numeraire of a vault lender cannot be
checked and is accepted as submitted.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext

from app.contracts.models import CatalogEntityType, CatalogEntry, PositionInput, PositionMode
from app.domain.snapshots import AssetInfo, MarketSnapshot, VaultSnapshot


USD = "USD"
VALUE_PLACES = 18
MAX_USD_EQUIVALENT = Decimal("1000000000000000")  # 1e15, written plainly
LTV_TOLERANCE = Decimal("0.01")
DECIMAL_PRECISION = 80  # enough for 1e15 with 18 places plus intermediate products
_VALUE_QUANTUM = Decimal(1).scaleb(-VALUE_PLACES)


class PositionValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class ValidatedPosition:
    """Structurally satisfies `app.domain.scenario.ScenarioPosition`."""

    mode: PositionMode
    entity_id: str
    numeraire: str
    amount: Decimal
    collateral_amount: Decimal | None
    debt_amount: Decimal | None
    ltv: Decimal | None
    collateral_value: Decimal | None
    debt_value: Decimal | None
    lltv: Decimal | None
    validated_at: datetime
    warnings: tuple[str, ...] = ()


def validate_position(
    position: PositionInput,
    entry: CatalogEntry,
    market: MarketSnapshot | None,
    *,
    vault: VaultSnapshot | None = None,
    now: datetime | None = None,
) -> ValidatedPosition:
    validated_at = _aware(now if now is not None else datetime.now(UTC))
    mode = _mode(position)
    _check_entry(position, entry, mode)
    _check_snapshots(entry, market, vault)

    if mode is PositionMode.LENDER:
        return _validate_lender(position, entry, market, vault, validated_at)
    return _validate_borrower(position, market, validated_at)


# Lender


def _validate_lender(
    position: PositionInput,
    entry: CatalogEntry,
    market: MarketSnapshot | None,
    vault: VaultSnapshot | None,
    validated_at: datetime,
) -> ValidatedPosition:
    for field in ("collateral_amount", "debt_amount", "ltv"):
        if getattr(position, field, None) is not None:
            raise PositionValidationError(field, "lender positions cannot include borrower fields")

    if market is not None:
        candidates: tuple[AssetInfo, ...] = (market.loan_asset,)
    elif vault is not None:
        candidates = (vault.asset,)
    else:
        candidates = ()
    numeraire = _resolve_numeraire(position.numeraire, candidates, strict=bool(candidates))
    unit = _asset_for(numeraire, candidates)

    raw_amount = _check_decimal("amount", position.amount)
    amount = _quantize_amount("amount", raw_amount, unit, ROUND_DOWN)
    _check_usd_equivalent("amount", amount, numeraire, unit)

    return ValidatedPosition(
        mode=PositionMode.LENDER,
        entity_id=entry.entity_id,
        numeraire=numeraire,
        amount=amount,
        collateral_amount=None,
        debt_amount=None,
        ltv=None,
        collateral_value=None,
        debt_value=None,
        lltv=market.lltv if market is not None else None,
        validated_at=validated_at,
    )


# Borrower


def _validate_borrower(
    position: PositionInput, market: MarketSnapshot | None, validated_at: datetime
) -> ValidatedPosition:
    if market is None:
        raise PositionValidationError("mode", "borrower positions require a market")
    for field in ("collateral_amount", "debt_amount", "ltv"):
        if getattr(position, field, None) is None:
            raise PositionValidationError(field, "borrower positions require collateral_amount, debt_amount, and ltv")

    loan = market.loan_asset
    collateral_asset = market.collateral_asset
    assets = (loan, collateral_asset)
    numeraire = _resolve_numeraire(position.numeraire, assets, strict=True)
    unit = _asset_for(numeraire, assets)

    raw_amount = _check_decimal("amount", position.amount)
    raw_collateral = _check_decimal("collateral_amount", position.collateral_amount)
    raw_debt = _check_decimal("debt_amount", position.debt_amount)
    ltv = _check_decimal("ltv", position.ltv)

    collateral_amount = _quantize_amount("collateral_amount", raw_collateral, collateral_asset, ROUND_DOWN)
    debt_amount = _quantize_amount("debt_amount", raw_debt, loan, ROUND_UP)
    amount = _quantize_amount("amount", raw_amount, unit, ROUND_UP)
    _check_usd_equivalent("collateral_amount", collateral_amount, collateral_asset.symbol, collateral_asset)
    _check_usd_equivalent("debt_amount", debt_amount, loan.symbol, loan)
    _check_usd_equivalent("amount", amount, numeraire, unit)

    lltv = market.lltv
    if ltv >= lltv:
        raise PositionValidationError(
            "ltv", f"declared LTV {ltv} is at or above the market LLTV {lltv}"
        )

    warnings: list[str] = []
    collateral_value = convert_to_numeraire(collateral_amount, collateral_asset, numeraire, assets, rounding=ROUND_DOWN)
    if collateral_value is None:
        warnings.append(_missing_price_warning("collateral", collateral_asset, numeraire, assets))
    debt_value = convert_to_numeraire(debt_amount, loan, numeraire, assets, rounding=ROUND_UP)
    if debt_value is None:
        warnings.append(_missing_price_warning("debt", loan, numeraire, assets))

    if collateral_value is not None and debt_value is not None:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            derived = debt_value / collateral_value
        if derived >= lltv:
            raise PositionValidationError(
                "ltv",
                f"LTV derived from snapshot prices ({derived:.6f}) is at or above the market LLTV {lltv}",
            )
        if abs(derived - ltv) > LTV_TOLERANCE:
            raise PositionValidationError(
                "ltv",
                f"declared LTV {ltv} is inconsistent with the LTV derived from snapshot prices "
                f"({derived:.6f}); the tolerance is {LTV_TOLERANCE}",
            )

    return ValidatedPosition(
        mode=PositionMode.BORROWER,
        entity_id=position.entity_id,
        numeraire=numeraire,
        amount=amount,
        collateral_amount=collateral_amount,
        debt_amount=debt_amount,
        ltv=ltv,
        collateral_value=collateral_value,
        debt_value=debt_value,
        lltv=lltv,
        validated_at=validated_at,
        warnings=tuple(warnings),
    )


# Entry and snapshot consistency


def _mode(position: PositionInput) -> PositionMode:
    try:
        return PositionMode(position.mode)
    except ValueError as error:
        raise PositionValidationError("mode", f"unsupported position mode {position.mode!r}") from error


def _check_entry(position: PositionInput, entry: CatalogEntry, mode: PositionMode) -> None:
    if position.entity_id != entry.entity_id:
        raise PositionValidationError(
            "entity_id", f"position entity_id {position.entity_id!r} does not match catalog entry {entry.entity_id!r}"
        )
    if entry.chain_id != 1:
        raise PositionValidationError("entity_id", "only Ethereum mainnet (chain 1) entries are supported")
    if not entry.supported:
        raise PositionValidationError("entity_id", f"catalog entry {entry.entity_id!r} is inactive; analysis is disabled")
    if mode not in entry.supported_modes:
        raise PositionValidationError(
            "mode", f"mode {mode.value!r} is not supported by catalog entry {entry.entity_id!r}"
        )


def _check_snapshots(entry: CatalogEntry, market: MarketSnapshot | None, vault: VaultSnapshot | None) -> None:
    if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
        if market is None:
            raise PositionValidationError("entity_id", "market entries require a market snapshot")
        if vault is not None:
            raise PositionValidationError("entity_id", "a vault snapshot was supplied for a market entry")
        if market.chain_id != entry.chain_id:
            raise PositionValidationError("entity_id", "market snapshot is from a different chain")
        if market.market_id.lower() != entry.address_or_market_id.lower():
            raise PositionValidationError(
                "entity_id", f"market snapshot {market.market_id} does not match catalog entry {entry.entity_id!r}"
            )
        if entry.lltv is not None and entry.lltv != market.lltv:
            raise PositionValidationError(
                "entity_id", f"catalog LLTV {entry.lltv} conflicts with market LLTV {market.lltv}"
            )
        if market.listed is False:
            raise PositionValidationError("entity_id", "market is no longer listed by Morpho; analysis is disabled")
        return

    if market is not None:
        raise PositionValidationError("entity_id", "a market snapshot was supplied for a vault entry")
    if vault is not None:
        if vault.chain_id != entry.chain_id:
            raise PositionValidationError("entity_id", "vault snapshot is from a different chain")
        if vault.address.lower() != entry.address_or_market_id.lower():
            raise PositionValidationError(
                "entity_id", f"vault snapshot {vault.address} does not match catalog entry {entry.entity_id!r}"
            )
        if vault.listed is False:
            raise PositionValidationError("entity_id", "vault is no longer listed by Morpho; analysis is disabled")


# Numeraire handling


def symbol_matches(numeraire: str, symbol: str) -> bool:
    return numeraire.strip().casefold() == symbol.strip().casefold()


def _resolve_numeraire(numeraire: str, candidates: Iterable[AssetInfo], *, strict: bool) -> str:
    cleaned = numeraire.strip()
    if not cleaned:
        raise PositionValidationError("numeraire", "numeraire must not be empty")
    if symbol_matches(cleaned, USD):
        return USD
    candidates = tuple(candidates)
    for asset in candidates:
        if symbol_matches(cleaned, asset.symbol):
            return asset.symbol
    if strict:
        allowed = ", ".join([USD, *(asset.symbol for asset in candidates)])
        raise PositionValidationError(
            "numeraire", f"numeraire {cleaned!r} is not supported for this position; use one of {allowed}"
        )
    return cleaned


def _asset_for(numeraire: str, candidates: Iterable[AssetInfo]) -> AssetInfo | None:
    for asset in candidates:
        if symbol_matches(numeraire, asset.symbol):
            return asset
    return None


def convert_to_numeraire(
    amount: Decimal,
    asset: AssetInfo,
    numeraire: str,
    reference_assets: Iterable[AssetInfo] = (),
    *,
    rounding: str = ROUND_DOWN,
) -> Decimal | None:
    """Express `amount` of `asset` in `numeraire`, or return None when a price is missing.

    `numeraire` is USD, the asset's own symbol, or the symbol of one of
    `reference_assets` (converted through USD prices). Nothing is guessed.
    """
    if symbol_matches(numeraire, asset.symbol):
        return quantize_value(amount, rounding)
    price = _usable_price(asset)
    if symbol_matches(numeraire, USD):
        return None if price is None else quantize_value(_multiply(amount, price), rounding)
    target = _asset_for(numeraire, reference_assets)
    target_price = None if target is None else _usable_price(target)
    if price is None or target_price is None:
        return None
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        value = amount * price / target_price
    return quantize_value(value, rounding)


def usd_equivalent(amount: Decimal, numeraire: str, unit: AssetInfo | None) -> Decimal | None:
    if symbol_matches(numeraire, USD):
        return amount
    price = None if unit is None else _usable_price(unit)
    return None if price is None else _multiply(amount, price)


def _usable_price(asset: AssetInfo) -> Decimal | None:
    """A price is usable only when it is a finite, strictly positive Decimal."""
    price = asset.price_usd
    if price is None or not price.is_finite() or price <= 0:
        return None
    return price


# Numeric checks and quantization


def _check_decimal(field: str, value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise PositionValidationError(field, f"{field} must be a Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise PositionValidationError(field, f"{field} must be a finite number")
    if value.as_tuple().exponent > 0:
        raise PositionValidationError(
            field, f"{field} must be written in plain decimal notation, not {value}"
        )
    if value <= 0:
        raise PositionValidationError(field, f"{field} must be greater than zero")
    if value > MAX_USD_EQUIVALENT:
        raise PositionValidationError(field, f"{field} {value} exceeds the maximum {MAX_USD_EQUIVALENT}")
    return value


def _quantize_amount(field: str, value: Decimal, unit: AssetInfo | None, rounding: str) -> Decimal:
    if unit is None:
        quantized = quantize_value(value, rounding)
    else:
        quantized = quantize_token_amount(value, unit.decimals, rounding)
    if quantized <= 0:
        raise PositionValidationError(field, f"{field} {value} is below the smallest representable unit")
    return quantized


def _check_usd_equivalent(field: str, amount: Decimal, numeraire: str, unit: AssetInfo | None) -> None:
    usd_value = usd_equivalent(amount, numeraire, unit)
    if usd_value is not None and usd_value > MAX_USD_EQUIVALENT:
        raise PositionValidationError(
            field, f"{field} USD-equivalent {usd_value} exceeds the maximum {MAX_USD_EQUIVALENT}"
        )


def quantize_value(value: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return value.quantize(_VALUE_QUANTUM, rounding=rounding)


def quantize_token_amount(value: Decimal, decimals: int, rounding: str) -> Decimal:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return left * right


def _missing_price_warning(
    leg: str, asset: AssetInfo, numeraire: str, assets: tuple[AssetInfo, ...]
) -> str:
    missing = [item.symbol for item in assets if _usable_price(item) is None]
    names = ", ".join(missing) if missing else asset.symbol
    return (
        f"The {leg} value could not be expressed in {numeraire}: the snapshot has no usable "
        f"USD price for {names}; it was left unknown, not estimated."
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("validation timestamp must be timezone-aware")
    return value
