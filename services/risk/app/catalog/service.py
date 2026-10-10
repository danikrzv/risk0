import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
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
from app.domain.snapshots import (
    DataEnvelope,
    MarketSnapshot,
    VaultSnapshot,
    VaultVersion,
)


MAINNET_CHAIN_ID = 1
# Entries hydrated at once. Each entry is a few sequential Morpho requests, so a
# cold catalog costs about entries / concurrency round trips instead of all of them.
DEFAULT_MAX_CONCURRENCY = 8

logger = logging.getLogger("risk0.catalog")
CatalogEnvelope = DataEnvelope[MarketSnapshot] | DataEnvelope[VaultSnapshot]


class CatalogService:
    """Hydrates the curated catalog from Morpho.

    Stale-but-usable cached snapshots are served at once while a background
    refresh replaces them; only entries without a usable cache wait on Morpho.
    """

    def __init__(
        self,
        repository: CatalogRepository,
        morpho: MorphoClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._repository = repository
        self._morpho = morpho
        self._clock = clock
        # Separate pools, so queued refreshes never delay a request's own fetches.
        self._fetches = ThreadPoolExecutor(
            max_concurrency, thread_name_prefix="catalog-fetch"
        )
        self._refreshes = ThreadPoolExecutor(
            max_concurrency, thread_name_prefix="catalog-refresh"
        )
        self._lock = threading.Lock()
        self._refreshing: dict[str, Future[None]] = {}
        # Outcome of each entry's latest background refresh.
        self._unreachable: set[str] = set()
        self._changed: set[str] = set()

    def list_hydrated(self) -> CatalogResponse:
        entries = self._repository.list_entries()
        return CatalogResponse(
            entries=tuple(self._fetches.map(self._hydrate, entries)),
            generated_at=self._clock(),
        )

    def wait_for_refreshes(self, timeout: float | None = None) -> None:
        """Block until the background refreshes started so far have finished."""
        with self._lock:
            pending = tuple(self._refreshing.values())
        wait(pending, timeout=timeout)

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
            envelope, refreshing = self._resolve(entry)
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
                f"{age_seconds} seconds old; "
                + (
                    "a refresh from the live source is in progress."
                    if refreshing
                    else "the live source is unavailable."
                )
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

    def _resolve(self, entry: CatalogEntry) -> tuple[CatalogEnvelope, bool]:
        """The entry's snapshot, and whether a background refresh will replace it."""
        key = _entry_key(entry)
        cached = self._cached(entry)
        if cached is not None and cached.freshness is FreshnessStatus.FRESH:
            return cached, False
        with self._lock:
            # The last refresh saw the entity vanish or conflict: let this
            # request see that outcome rather than the older snapshot.
            changed = key in self._changed
            self._changed.discard(key)
        if cached is None or changed:
            return self._fetch(entry), False
        with self._lock:
            unreachable = key in self._unreachable
        self._schedule_refresh(entry, key)
        return cached, not unreachable

    def _schedule_refresh(self, entry: CatalogEntry, key: str) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            future = self._refreshes.submit(self._refresh, entry, key)
            self._refreshing[key] = future

    def _refresh(self, entry: CatalogEntry, key: str) -> None:
        unreachable = changed = False
        try:
            envelope = self._fetch(entry)
        except (MorphoNotFoundError, MorphoDataConflictError):
            changed = True
        except MorphoUnavailableError:
            unreachable = True
        except Exception as error:  # a background thread has no caller to raise to
            logger.warning("catalog refresh failed: %s", type(error).__name__)
            unreachable = True
        else:
            # MorphoClient falls back to the stale cache when Morpho fails.
            unreachable = envelope.freshness is not FreshnessStatus.FRESH
        finally:
            with self._lock:
                self._refreshing.pop(key, None)
                for outcomes, flag in (
                    (self._unreachable, unreachable),
                    (self._changed, changed),
                ):
                    if flag:
                        outcomes.add(key)
                    else:
                        outcomes.discard(key)

    def _cached(self, entry: CatalogEntry) -> CatalogEnvelope | None:
        identifier = entry.address_or_market_id
        chain_id = entry.chain_id
        if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
            return self._morpho.cached_market(identifier, chain_id=chain_id)
        return self._morpho.cached_vault(
            identifier, _vault_version(entry), chain_id=chain_id
        )

    def _fetch(self, entry: CatalogEntry) -> CatalogEnvelope:
        identifier = entry.address_or_market_id
        chain_id = entry.chain_id
        if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
            return self._morpho.get_market(identifier, chain_id=chain_id)
        return self._morpho.get_vault(
            identifier, _vault_version(entry), chain_id=chain_id
        )


def _entry_key(entry: CatalogEntry) -> str:
    return (
        f"{entry.entity_type.value}:{entry.chain_id}:"
        f"{entry.address_or_market_id.lower()}"
    )


def _vault_version(entry: CatalogEntry) -> VaultVersion:
    return "v2" if entry.entity_type is CatalogEntityType.MORPHO_VAULT_V2 else "v1"


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
