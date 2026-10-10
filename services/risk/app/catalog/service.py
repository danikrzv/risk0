from collections.abc import Callable
from datetime import UTC, datetime

from app.adapters.morpho import (
    MorphoClient,
    MorphoDataConflictError,
    MorphoNotFoundError,
    MorphoUnavailableError,
)
from app.catalog.repository import CatalogRepository
from app.contracts.models import (
    CatalogAvailability,
    CatalogEntityType,
    CatalogEntry,
    CatalogMetrics,
    CatalogResponse,
    FreshnessStatus,
    HydratedCatalogEntry,
)
from app.domain.snapshots import DataEnvelope, MarketSnapshot, VaultSnapshot


MAINNET_CHAIN_ID = 1


class CatalogService:
    def __init__(
        self,
        repository: CatalogRepository,
        morpho: MorphoClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._morpho = morpho
        self._clock = clock

    def list_hydrated(self) -> CatalogResponse:
        return CatalogResponse(
            entries=tuple(
                self._hydrate(entry) for entry in self._repository.list_entries()
            ),
            generated_at=self._clock(),
        )

    def _hydrate(self, entry: CatalogEntry) -> HydratedCatalogEntry:
        hydrated = self._hydrate_from_morpho(entry)
        if entry.supported:
            return hydrated
        # Unsupported entries stay listed and are hydrated for display only.
        return hydrated.model_copy(
            update={
                "analysis_available": False,
                "availability": CatalogAvailability.CATALOG_INACTIVE,
                "warnings": (_inactive_reason(entry), *hydrated.warnings),
            }
        )

    def _hydrate_from_morpho(self, entry: CatalogEntry) -> HydratedCatalogEntry:
        try:
            envelope = self._fetch(entry)
        except MorphoNotFoundError:
            return _unavailable(
                entry,
                CatalogAvailability.NOT_FOUND,
                "Morpho no longer resolves this entity; analysis is disabled.",
            )
        except MorphoDataConflictError:
            return _unavailable(
                entry,
                CatalogAvailability.DATA_CONFLICT,
                "Morpho sources returned conflicting data; analysis is disabled.",
            )
        except MorphoUnavailableError:
            return _unavailable(
                entry,
                CatalogAvailability.PROVIDER_UNAVAILABLE,
                "Morpho data is unavailable and no usable cache exists.",
            )

        snapshot = envelope.value
        availability = CatalogAvailability.AVAILABLE
        warnings: list[str] = []
        if isinstance(snapshot, MarketSnapshot) and entry.lltv != snapshot.lltv:
            availability = CatalogAvailability.DATA_CONFLICT
            warnings.append(
                f"Catalog LLTV {entry.lltv} differs from Morpho LLTV {snapshot.lltv}."
            )
        elif snapshot.listed is False:
            availability = CatalogAvailability.DELISTED
            warnings.append("Morpho no longer lists this entity; analysis is disabled.")
        else:
            critical_missing = _critical_missing(snapshot)
            if critical_missing:
                availability = CatalogAvailability.CRITICAL_DATA_MISSING
                warnings.append(
                    "Critical Morpho fields are missing; analysis is disabled: "
                    + ", ".join(critical_missing)
                    + "."
                )

        age_seconds = max(
            0, int((self._clock() - envelope.fetched_at).total_seconds())
        )
        if envelope.freshness is not FreshnessStatus.FRESH:
            warnings.append(
                f"Morpho data is {envelope.freshness.value} cached data, "
                f"{age_seconds} seconds old; the live source is unavailable."
            )
        usable = envelope.freshness is not FreshnessStatus.EXPIRED

        return HydratedCatalogEntry(
            **entry.model_dump(),
            analysis_available=usable
            and availability is CatalogAvailability.AVAILABLE,
            availability=availability,
            metrics=_metrics(snapshot),
            source=envelope.source,
            fetched_at=envelope.fetched_at,
            freshness=envelope.freshness,
            age_seconds=age_seconds,
            block_number=envelope.block_number,
            warnings=tuple(warnings),
        )

    def _fetch(
        self, entry: CatalogEntry
    ) -> DataEnvelope[MarketSnapshot] | DataEnvelope[VaultSnapshot]:
        identifier = entry.address_or_market_id
        chain_id = entry.chain_id
        if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
            return self._morpho.get_market(identifier, chain_id=chain_id)
        if entry.entity_type is CatalogEntityType.MORPHO_VAULT_V2:
            return self._morpho.get_vault(identifier, "v2", chain_id=chain_id)
        return self._morpho.get_vault(identifier, "v1", chain_id=chain_id)


def _inactive_reason(entry: CatalogEntry) -> str:
    if entry.chain_id != MAINNET_CHAIN_ID:
        return (
            f"Risk0 analysis is Ethereum mainnet-only; this chain {entry.chain_id} "
            "entry is listed for reference and analysis is disabled."
        )
    return "Entry is inactive in the curated catalog; analysis is disabled."


def _metrics(snapshot: MarketSnapshot | VaultSnapshot) -> CatalogMetrics:
    if isinstance(snapshot, MarketSnapshot):
        return CatalogMetrics(
            asset_symbol=snapshot.loan_asset.symbol,
            collateral_symbol=snapshot.collateral_asset.symbol,
            total_assets=snapshot.total_supply_assets,
            total_assets_usd=snapshot.total_supply_usd,
            liquidity_assets=snapshot.liquidity_assets,
            liquidity_usd=snapshot.liquidity_usd,
            utilization=snapshot.utilization,
        )
    return CatalogMetrics(
        asset_symbol=snapshot.asset.symbol,
        collateral_symbol=None,
        total_assets=snapshot.total_assets,
        total_assets_usd=snapshot.total_assets_usd,
        liquidity_assets=snapshot.liquidity_assets,
        liquidity_usd=snapshot.liquidity_usd,
        utilization=None,
    )


def _critical_missing(snapshot: MarketSnapshot | VaultSnapshot) -> tuple[str, ...]:
    if isinstance(snapshot, MarketSnapshot):
        fields = (
            ("loan_asset.price_usd", snapshot.loan_asset.price_usd),
            ("collateral_asset.price_usd", snapshot.collateral_asset.price_usd),
            ("total_supply_usd", snapshot.total_supply_usd),
            ("total_borrow_usd", snapshot.total_borrow_usd),
            ("liquidity_usd", snapshot.liquidity_usd),
            ("utilization", snapshot.utilization),
            ("listed", snapshot.listed),
        )
    else:
        fields = (
            ("asset.price_usd", snapshot.asset.price_usd),
            ("total_assets_usd", snapshot.total_assets_usd),
            ("liquidity_usd", snapshot.liquidity_usd),
            ("listed", snapshot.listed),
        )
    return tuple(name for name, value in fields if value is None)


def _unavailable(
    entry: CatalogEntry, availability: CatalogAvailability, warning: str
) -> HydratedCatalogEntry:
    return HydratedCatalogEntry(
        **entry.model_dump(),
        analysis_available=False,
        availability=availability,
        metrics=None,
        source=None,
        fetched_at=None,
        freshness=None,
        block_number=None,
        warnings=(warning,),
    )
