from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from app.adapters.morpho import (
    MorphoClient,
    MorphoDataConflictError,
    MorphoNotFoundError,
    MorphoUnavailableError,
)
from app.catalog.repository import CatalogRepository
from app.contracts.models import (
    CatalogEntityType,
    CatalogEntry,
    ContractModel,
    DecimalValue,
    FreshnessStatus,
)
from app.domain.snapshots import DataEnvelope, MarketSnapshot, VaultSnapshot


class CatalogAvailability(StrEnum):
    AVAILABLE = "available"
    CATALOG_INACTIVE = "catalog_inactive"
    NOT_FOUND = "not_found"
    DELISTED = "delisted"
    DATA_CONFLICT = "data_conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class CatalogMetrics(ContractModel):
    asset_symbol: str
    collateral_symbol: str | None
    total_assets: DecimalValue
    total_assets_usd: DecimalValue | None
    liquidity_assets: DecimalValue
    liquidity_usd: DecimalValue | None
    utilization: DecimalValue | None


class HydratedCatalogEntry(CatalogEntry):
    analysis_available: bool
    availability: CatalogAvailability
    metrics: CatalogMetrics | None
    source: str | None
    fetched_at: datetime | None
    freshness: FreshnessStatus | None
    age_seconds: int | None = Field(default=None, ge=0)
    block_number: int | None = Field(default=None, ge=0)
    warnings: tuple[str, ...] = ()


class HydratedCatalogResponse(ContractModel):
    entries: tuple[HydratedCatalogEntry, ...]
    generated_at: datetime


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

    def list_hydrated(self) -> HydratedCatalogResponse:
        return HydratedCatalogResponse(
            entries=tuple(
                self._hydrate(entry) for entry in self._repository.list_entries()
            ),
            generated_at=self._clock(),
        )

    def _hydrate(self, entry: CatalogEntry) -> HydratedCatalogEntry:
        if not entry.supported:
            return _unavailable(
                entry,
                CatalogAvailability.CATALOG_INACTIVE,
                "Entry is inactive in the curated catalog; analysis is disabled.",
            )
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
        if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
            return self._morpho.get_market(identifier)
        if entry.entity_type is CatalogEntityType.MORPHO_VAULT_V2:
            return self._morpho.get_vault(identifier, "v2")
        return self._morpho.get_vault(identifier, "v1")


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
