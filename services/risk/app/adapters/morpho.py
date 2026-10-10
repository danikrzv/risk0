import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

from app.config import CachePolicy
from app.contracts.models import DecimalValue, FreshnessStatus, HexIdentifier
from app.domain.snapshots import (
    AllocationSnapshot,
    AssetInfo,
    DataEnvelope,
    MarketSnapshot,
    SnapshotModel,
    VaultSnapshot,
    VaultVersion,
)
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


DEFAULT_BASE_URL = "https://api.morpho.org"
CHAIN_ID = 1
CACHE_NAMESPACE = "morpho"
SNAPSHOT_POLICY = CachePolicy(ttl=timedelta(seconds=60), retention=timedelta(hours=24))
DISCOVERY_POLICY = CachePolicy(ttl=timedelta(minutes=30), retention=timedelta(hours=24))
DEFAULT_DISCOVERY_PAGE_SIZE = 100
# Nested adapter positions make Vault V2 pages expensive under Morpho's complexity cap.
MAX_VAULT_V2_PAGE_SIZE = 20
DEFAULT_MAX_POSITIONS_PER_MARKET = 2000
MAX_DISCOVERY_PAGES = 500
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD_DECIMALS = 18
RAY_DECIMALS = 27

_ASSET_FIELDS = "address symbol decimals price { usd }"
MARKET_QUERY = (
    "query MarketMetadata($marketId: String!, $chainId: Int!) { "
    "marketById(marketId: $marketId, chainId: $chainId) { "
    f"marketId listed lltv loanAsset {{ {_ASSET_FIELDS} }} "
    f"collateralAsset {{ {_ASSET_FIELDS} }} "
    "state { supplyAssetsUsd borrowAssetsUsd liquidityAssetsUsd utilization } } }"
)
VAULT_V1_QUERY = (
    "query VaultV1Metadata($address: String!, $chainId: Int!) { "
    "vaultByAddress(address: $address, chainId: $chainId) { "
    f"address listed asset {{ {_ASSET_FIELDS} }} state {{ totalAssetsUsd }} }} }}"
)
VAULT_V2_QUERY = (
    "query VaultV2Metadata($address: String!, $chainId: Int!) { "
    "vaultV2ByAddress(address: $address, chainId: $chainId) { "
    f"address listed asset {{ {_ASSET_FIELDS} }} totalAssetsUsd liquidityUsd }} }}"
)

_DISCOVERY_ASSET_FIELDS = "address symbol decimals price { usd }"
_PAGE_INFO = "pageInfo { count countTotal limit skip }"
DISCOVERY_CHAINS_QUERY = "query MorphoChains { chains { id network } }"
_DISCOVERY_MARKET_FIELDS = (
    "marketId listed lltv irmAddress oracle { address type } "
    f"loanAsset {{ {_DISCOVERY_ASSET_FIELDS} }} "
    f"collateralAsset {{ {_DISCOVERY_ASSET_FIELDS} }} badDebt {{ underlying usd }} "
    "state { blockNumber timestamp supplyAssets borrowAssets liquidityAssets "
    "collateralAssets supplyAssetsUsd borrowAssetsUsd liquidityAssetsUsd "
    "collateralAssetsUsd utilization price }"
)
_DISCOVERY_VAULT_V1_FIELDS = (
    f"address name symbol listed asset {{ {_DISCOVERY_ASSET_FIELDS} }} "
    "liquidity { underlying usd } state { blockNumber timestamp curator totalAssets "
    "totalAssetsUsd allocation { supplyAssets supplyAssetsUsd supplyCap "
    "market { marketId } } }"
)
_DISCOVERY_VAULT_V2_FIELDS = (
    f"address name symbol listed asset {{ {_DISCOVERY_ASSET_FIELDS} }} "
    "curator { address } totalAssets totalAssetsUsd liquidity liquidityUsd idleAssets "
    "adapters(first: 10) { items { address type assets "
    "... on MorphoMarketV1Adapter { positions(first: 50) { items { "
    "market { marketId } state { supplyAssets supplyAssetsUsd } } } } "
    "... on MetaMorphoAdapter { metaMorpho { address } } } }"
)
_POSITION_FIELDS = (
    "healthFactor user { address } "
    "market { marketId loanAsset { decimals } collateralAsset { decimals } } "
    "state { supplyAssets borrowAssets collateral }"
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class DiscoveredAsset(SnapshotModel):
    address: HexIdentifier
    symbol: str
    decimals: int
    price_usd: DecimalValue | None


class DiscoveredMarket(SnapshotModel):
    chain_id: int
    market_id: HexIdentifier
    listed: bool | None
    lltv: DecimalValue
    loan_asset: DiscoveredAsset
    collateral_asset: DiscoveredAsset | None
    oracle_address: HexIdentifier | None
    oracle_type: str | None
    irm_address: HexIdentifier | None
    supply_assets: DecimalValue
    borrow_assets: DecimalValue
    liquidity_assets: DecimalValue
    collateral_assets: DecimalValue | None
    supply_usd: DecimalValue | None
    borrow_usd: DecimalValue | None
    liquidity_usd: DecimalValue | None
    collateral_usd: DecimalValue | None
    utilization: DecimalValue | None
    bad_debt_assets: DecimalValue | None
    bad_debt_usd: DecimalValue | None
    # Collateral priced in loan-asset units, as the market oracle reports it.
    oracle_price: DecimalValue | None
    state_block_number: int | None
    state_timestamp: int | None


class DiscoveredAllocation(SnapshotModel):
    market_id: HexIdentifier | None
    vault_address: HexIdentifier | None = None
    adapter_address: HexIdentifier | None = None
    supply_assets: DecimalValue
    supply_usd: DecimalValue | None = None
    supply_cap: DecimalValue | None = None


class DiscoveredVault(SnapshotModel):
    chain_id: int
    address: HexIdentifier
    version: VaultVersion
    name: str
    symbol: str
    listed: bool | None
    asset: DiscoveredAsset
    curator: HexIdentifier | None
    total_assets: DecimalValue
    total_assets_usd: DecimalValue | None
    liquidity_assets: DecimalValue | None
    liquidity_usd: DecimalValue | None
    idle_assets: DecimalValue | None
    allocations: tuple[DiscoveredAllocation, ...]
    # Assets that Morpho does not attribute to a market or vault.
    unmodelled_assets: DecimalValue
    state_timestamp: int | None


class RejectedItem(SnapshotModel):
    identifier: str
    reason: str


class MarketDiscovery(SnapshotModel):
    items: tuple[DiscoveredMarket, ...]
    rejected: tuple[RejectedItem, ...] = ()


class VaultDiscovery(SnapshotModel):
    items: tuple[DiscoveredVault, ...]
    rejected: tuple[RejectedItem, ...] = ()


class MorphoChain(SnapshotModel):
    chain_id: int
    network: str


class MarketPosition(SnapshotModel):
    user: HexIdentifier
    supply_assets: DecimalValue
    borrow_assets: DecimalValue
    collateral_assets: DecimalValue
    health_factor: DecimalValue | None


class MarketPositions(SnapshotModel):
    chain_id: int
    market_id: HexIdentifier
    positions: tuple[MarketPosition, ...]
    # False when the position list was truncated or an item could not be parsed.
    complete: bool


class MorphoError(Exception):
    """Base error for Morpho data access."""


class MorphoUnavailableError(MorphoError):
    """Morpho could not be reached or returned unusable data, and no cache applies."""


class MorphoDataConflictError(MorphoUnavailableError):
    """Morpho sources disagree; cached values are not used to paper over it."""


class MorphoNotFoundError(MorphoError):
    """Morpho no longer resolves the requested entity."""


class _ProviderError(Exception):
    pass


class _DataConflictError(_ProviderError):
    pass


class MorphoClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        http_client: httpx.Client | None = None,
        cache: SQLiteCache | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = 10,
        max_attempts: int = 3,
        backoff_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        discovery_page_size: int = DEFAULT_DISCOVERY_PAGE_SIZE,
        max_positions_per_market: int = DEFAULT_MAX_POSITIONS_PER_MARKET,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if discovery_page_size < 1 or max_positions_per_market < 1:
            raise ValueError("page size and position cap must be positive")
        self._base_url = _public_base_url(base_url)
        self._http = http_client or httpx.Client(
            headers={"user-agent": "risk0-risk/0.1.0"}
        )
        self._cache = cache
        self._clock = clock
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._page_size = discovery_page_size
        self._max_positions = max_positions_per_market

    def get_market(
        self, market_id: str, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[MarketSnapshot]:
        return self._load(
            _market_key(market_id, chain_id),
            DataEnvelope[MarketSnapshot],
            lambda: self._fetch_market(market_id, chain_id),
        )

    def get_vault(
        self, address: str, version: VaultVersion, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[VaultSnapshot]:
        return self._load(
            _vault_key(address, version, chain_id),
            DataEnvelope[VaultSnapshot],
            lambda: self._fetch_vault(address, version, chain_id),
        )

    def cached_market(
        self, market_id: str, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[MarketSnapshot] | None:
        """The cached `get_market` snapshot while usable (fresh or stale); no request."""
        return self._cached(
            _market_key(market_id, chain_id), DataEnvelope[MarketSnapshot]
        )

    def cached_vault(
        self, address: str, version: VaultVersion, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[VaultSnapshot] | None:
        """The cached `get_vault` snapshot while usable (fresh or stale); no request."""
        return self._cached(
            _vault_key(address, version, chain_id), DataEnvelope[VaultSnapshot]
        )

    def get_allocations(
        self, address: str, version: VaultVersion, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[tuple[AllocationSnapshot, ...]]:
        return self._load(
            f"allocations_{version}:{chain_id}:{address.lower()}",
            DataEnvelope[tuple[AllocationSnapshot, ...]],
            lambda: self._fetch_allocations(address, version, chain_id),
        )

    def list_chains(self) -> DataEnvelope[tuple[MorphoChain, ...]]:
        return self._load(
            "chains",
            DataEnvelope[tuple[MorphoChain, ...]],
            self._fetch_chains,
            policy=DISCOVERY_POLICY,
        )

    def discover_markets(
        self, chain_id: int, *, market_ids: Sequence[str] | None = None
    ) -> DataEnvelope[MarketDiscovery]:
        """All markets of a chain, or a monitored subset, read page by page."""
        return self._load(
            f"discovery_markets:{chain_id}:{_subset_key(market_ids)}",
            DataEnvelope[MarketDiscovery],
            lambda: self._fetch_market_discovery(chain_id, market_ids),
            policy=DISCOVERY_POLICY if market_ids is None else SNAPSHOT_POLICY,
        )

    def discover_vaults(
        self, chain_id: int, *, addresses: Sequence[str] | None = None
    ) -> DataEnvelope[VaultDiscovery]:
        """All Vault V1 and V2 vaults of a chain, or a monitored subset."""
        return self._load(
            f"discovery_vaults:{chain_id}:{_subset_key(addresses)}",
            DataEnvelope[VaultDiscovery],
            lambda: self._fetch_vault_discovery(chain_id, addresses),
            policy=DISCOVERY_POLICY if addresses is None else SNAPSHOT_POLICY,
        )

    def get_market_positions(
        self, market_id: str, *, chain_id: int = CHAIN_ID
    ) -> DataEnvelope[MarketPositions]:
        return self._load(
            f"positions:{chain_id}:{market_id.lower()}",
            DataEnvelope[MarketPositions],
            lambda: self._fetch_positions(market_id, chain_id),
        )

    def _cached(self, key: str, envelope_type: type[ModelT]) -> ModelT | None:
        if self._cache is None:
            return None
        cached = self._cache.get(CACHE_NAMESPACE, key, self._clock())
        if cached is None or cached.freshness is Freshness.EXPIRED:
            return None
        return _restore(envelope_type, cached.payload, cached.freshness)

    def _load(
        self,
        key: str,
        envelope_type: type[ModelT],
        fetch: Callable[[], ModelT],
        *,
        policy: CachePolicy = SNAPSHOT_POLICY,
    ) -> ModelT:
        cached = (
            self._cache.get(CACHE_NAMESPACE, key, self._clock())
            if self._cache is not None
            else None
        )
        if cached is not None and cached.freshness is Freshness.FRESH:
            return _restore(envelope_type, cached.payload, cached.freshness)

        try:
            envelope = fetch()
        except _DataConflictError as error:
            raise MorphoDataConflictError(
                f"Morpho data conflict for {key}: {error}"
            ) from error
        except (
            _ProviderError,
            DecimalException,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            if cached is not None and cached.freshness is not Freshness.EXPIRED:
                return _restore(envelope_type, cached.payload, cached.freshness)
            raise MorphoUnavailableError(
                f"Morpho data unavailable for {key}: {error}"
            ) from error

        if self._cache is not None:
            self._cache.set(
                CACHE_NAMESPACE,
                key,
                envelope.model_dump_json().encode(),
                envelope.fetched_at,
                policy,
            )
        return envelope

    def _fetch_market(
        self, market_id: str, chain_id: int
    ) -> DataEnvelope[MarketSnapshot]:
        fetched_at = self._clock()
        selector = f"{chain_id}:{market_id}"
        config_path = f"/v0/blue/markets/{selector}"
        state_path = f"{config_path}/state"
        config = self._get(config_path)
        state = self._get(state_path)
        metadata = self._graphql(
            MARKET_QUERY, {"marketId": market_id, "chainId": chain_id}, "marketById"
        )

        for payload in (config, state):
            _require_identity(payload, "market_id", market_id, chain_id)
        lltv = _units(config["lltv_wad"], WAD_DECIMALS)
        if metadata.get("lltv") is not None and _units(
            metadata["lltv"], WAD_DECIMALS
        ) != lltv:
            raise _DataConflictError(
                f"lltv conflict between REST ({lltv}) and GraphQL "
                f"({_units(metadata['lltv'], WAD_DECIMALS)})"
            )
        loan_asset = _asset(metadata["loanAsset"], expected=config["loan_token"])
        collateral_asset = _asset(
            metadata["collateralAsset"], expected=config["collateral_token"]
        )

        supply = _units(state["total_supply_assets"], loan_asset.decimals)
        borrow = _units(state["total_borrow_assets"], loan_asset.decimals)
        if borrow > supply:
            raise _DataConflictError("market borrow assets exceed supply assets")
        usd = metadata.get("state") or {}

        return DataEnvelope[MarketSnapshot](
            value=MarketSnapshot(
                chain_id=chain_id,
                market_id=config["market_id"],
                loan_asset=loan_asset,
                collateral_asset=collateral_asset,
                oracle_address=config["oracle_address"],
                irm_address=config["irm_address"],
                lltv=lltv,
                total_supply_assets=supply,
                total_borrow_assets=borrow,
                liquidity_assets=supply - borrow,
                utilization=borrow / supply if supply else None,
                total_supply_usd=_optional_decimal(usd.get("supplyAssetsUsd")),
                total_borrow_usd=_optional_decimal(usd.get("borrowAssetsUsd")),
                liquidity_usd=_optional_decimal(usd.get("liquidityAssetsUsd")),
                listed=metadata.get("listed"),
                last_accrual_timestamp=state.get("last_accrual_timestamp"),
                sources=(
                    self._url(config_path),
                    self._url(state_path),
                    self._url("/graphql"),
                ),
            ),
            source=self._url(state_path),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
            block_number=int(state["last_indexed_block"]),
        )

    def _fetch_vault(
        self, address: str, version: VaultVersion, chain_id: int
    ) -> DataEnvelope[VaultSnapshot]:
        fetched_at = self._clock()
        config_path = _vault_path(address, version, chain_id)
        state_path = (
            f"/v1/vaults-v2/{chain_id}:{address}/state"
            if version == "v2"
            else f"{config_path}/state"
        )
        config = self._get(config_path)
        state = self._get(state_path)
        if version == "v2":
            metadata = self._graphql(
                VAULT_V2_QUERY,
                {"address": address, "chainId": chain_id},
                "vaultV2ByAddress",
            )
            total_assets_usd = metadata.get("totalAssetsUsd")
            liquidity_usd = metadata.get("liquidityUsd")
        else:
            metadata = self._graphql(
                VAULT_V1_QUERY,
                {"address": address, "chainId": chain_id},
                "vaultByAddress",
            )
            total_assets_usd = (metadata.get("state") or {}).get("totalAssetsUsd")
            liquidity_usd = None

        for payload in (config, state):
            _require_identity(payload, "address", address, chain_id)
        asset = _asset(config["asset"], price_source=metadata["asset"])
        decimals = asset.decimals

        return DataEnvelope[VaultSnapshot](
            value=VaultSnapshot(
                chain_id=chain_id,
                address=config["address"],
                version=version,
                name=config["name"],
                symbol=config["symbol"],
                asset=asset,
                total_assets=_units(state["total_assets"], decimals),
                liquidity_assets=_units(state["withdrawable_assets"], decimals),
                idle_assets=(
                    _units(state["idle_assets"], decimals)
                    if state.get("idle_assets") is not None
                    else None
                ),
                share_price=_units(state["share_price_ray"], RAY_DECIMALS),
                total_assets_usd=_optional_decimal(total_assets_usd),
                liquidity_usd=(
                    _optional_decimal(liquidity_usd)
                    if liquidity_usd is not None
                    else _derived_usd(
                        _units(state["withdrawable_assets"], decimals),
                        asset.price_usd,
                    )
                ),
                listed=metadata.get("listed"),
                sources=(
                    self._url(config_path),
                    self._url(state_path),
                    self._url("/graphql"),
                ),
            ),
            source=self._url(state_path),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
            block_number=int(state["last_indexed_block"]),
        )

    def _fetch_allocations(
        self, address: str, version: VaultVersion, chain_id: int
    ) -> DataEnvelope[tuple[AllocationSnapshot, ...]]:
        fetched_at = self._clock()
        config_path = _vault_path(address, version, chain_id)
        allocations_path = f"{config_path}/allocations"
        config = self._get(config_path)
        payload = self._get(allocations_path)
        _require_identity(config, "address", address, chain_id)
        _require_identity(payload, "vault_address", address, chain_id)
        decimals = int(config["asset"]["decimals"])

        if version == "v2":
            rows, provider_total_raw = _v2_allocation_rows(
                payload["allocations"], decimals
            )
        else:
            state = self._get(f"{config_path}/state")
            _require_identity(state, "address", address, chain_id)
            rows = _v1_allocation_rows(payload["allocations"], decimals)
            provider_total_raw = _nonnegative_integer(state["total_assets"])

        selected_total_raw = sum((row[5] for row in rows), 0)
        if version == "v1" and selected_total_raw > provider_total_raw + 1:
            raise _DataConflictError("V1 allocations exceed vault total_assets")

        total = _units(provider_total_raw, decimals)
        allocations = []
        with localcontext() as context:
            context.prec = 60
            for market_id, adapter, assets, cap, pending_removal, _ in rows:
                allocations.append(
                    AllocationSnapshot(
                        vault_address=payload["vault_address"],
                        market_id=market_id,
                        adapter_address=adapter,
                        supply_assets=assets,
                        supply_cap=cap,
                        weight=assets / total if total else Decimal(0),
                        pending_removal=pending_removal,
                    )
                )

        return DataEnvelope[tuple[AllocationSnapshot, ...]](
            value=tuple(allocations),
            source=self._url(allocations_path),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
            block_number=int(payload["last_indexed_block"]),
        )

    def _fetch_chains(self) -> DataEnvelope[tuple[MorphoChain, ...]]:
        fetched_at = self._clock()
        body = self._request("POST", "/graphql", {"query": DISCOVERY_CHAINS_QUERY})
        if body.get("errors"):
            raise _ProviderError("Morpho GraphQL returned errors for chains")
        chains = (body.get("data") or {}).get("chains")
        if not isinstance(chains, list):
            raise _ProviderError("Morpho GraphQL returned no chains")
        return DataEnvelope[tuple[MorphoChain, ...]](
            value=tuple(
                MorphoChain(chain_id=int(chain["id"]), network=str(chain["network"]))
                for chain in chains
            ),
            source=self._url("/graphql"),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
        )

    def _fetch_market_discovery(
        self, chain_id: int, market_ids: Sequence[str] | None
    ) -> DataEnvelope[MarketDiscovery]:
        fetched_at = self._clock()
        variables: dict[str, object] = {"chainIds": [chain_id]}
        where = "chainId_in: $chainIds"
        declared = "$chainIds: [Int!]"
        if market_ids is not None:
            variables["marketIds"] = list(market_ids)
            where += ", uniqueKey_in: $marketIds"
            declared += ", $marketIds: [String!]"
        query = (
            f"query DiscoverMarkets({declared}, $first: Int!, $skip: Int!) {{ "
            f"markets(first: $first, skip: $skip, where: {{ {where} }}, "
            "orderBy: UniqueKey, orderDirection: Asc) { "
            f"items {{ {_DISCOVERY_MARKET_FIELDS} }} {_PAGE_INFO} }} }}"
        )
        raw_items, _ = self._paginate("markets", query, variables, self._page_size)
        items, rejected = _parse_items(
            raw_items,
            lambda raw: _discovered_market(raw, chain_id),
            lambda raw: str(raw.get("marketId")),
        )
        return DataEnvelope[MarketDiscovery](
            value=MarketDiscovery(items=items, rejected=rejected),
            source=self._url("/graphql"),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
            block_number=max(
                (m.state_block_number for m in items if m.state_block_number is not None),
                default=None,
            ),
        )

    def _fetch_vault_discovery(
        self, chain_id: int, addresses: Sequence[str] | None
    ) -> DataEnvelope[VaultDiscovery]:
        fetched_at = self._clock()
        variables: dict[str, object] = {"chainIds": [chain_id]}
        where = "chainId_in: $chainIds"
        declared = "$chainIds: [Int!]"
        if addresses is not None:
            variables["addresses"] = list(addresses)
            where += ", address_in: $addresses"
            declared += ", $addresses: [String!]"
        v1_query = (
            f"query DiscoverVaults({declared}, $first: Int!, $skip: Int!) {{ "
            f"vaults(first: $first, skip: $skip, where: {{ {where} }}, "
            "orderBy: Address, orderDirection: Asc) { "
            f"items {{ {_DISCOVERY_VAULT_V1_FIELDS} }} {_PAGE_INFO} }} }}"
        )
        v2_query = (
            f"query DiscoverVaultV2s({declared}, $first: Int!, $skip: Int!) {{ "
            f"vaultV2s(first: $first, skip: $skip, where: {{ {where} }}, "
            "orderBy: Address, orderDirection: Asc) { "
            f"items {{ {_DISCOVERY_VAULT_V2_FIELDS} }} {_PAGE_INFO} }} }}"
        )
        v1_raw, _ = self._paginate("vaults", v1_query, variables, self._page_size)
        v2_raw, _ = self._paginate(
            "vaultV2s", v2_query, variables, min(self._page_size, MAX_VAULT_V2_PAGE_SIZE)
        )
        v1_items, v1_rejected = _parse_items(
            v1_raw,
            lambda raw: _discovered_vault_v1(raw, chain_id),
            lambda raw: str(raw.get("address")),
        )
        v2_items, v2_rejected = _parse_items(
            v2_raw,
            lambda raw: _discovered_vault_v2(raw, chain_id),
            lambda raw: str(raw.get("address")),
        )
        return DataEnvelope[VaultDiscovery](
            value=VaultDiscovery(
                items=v1_items + v2_items, rejected=v1_rejected + v2_rejected
            ),
            source=self._url("/graphql"),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
        )

    def _fetch_positions(
        self, market_id: str, chain_id: int
    ) -> DataEnvelope[MarketPositions]:
        fetched_at = self._clock()
        query = (
            "query MarketPositions($chainIds: [Int!], $marketIds: [String!], "
            "$first: Int!, $skip: Int!) { marketPositions(first: $first, skip: $skip, "
            "where: { chainId_in: $chainIds, marketUniqueKey_in: $marketIds }, "
            "orderBy: BorrowShares, orderDirection: Desc) { "
            f"items {{ {_POSITION_FIELDS} }} {_PAGE_INFO} }} }}"
        )
        raw_items, exhausted = self._paginate(
            "marketPositions",
            query,
            {"chainIds": [chain_id], "marketIds": [market_id]},
            self._page_size,
            limit=self._max_positions,
        )
        positions, rejected = _parse_items(
            raw_items,
            lambda raw: _market_position(raw, market_id),
            lambda raw: str((raw.get("user") or {}).get("address")),
        )
        return DataEnvelope[MarketPositions](
            value=MarketPositions(
                chain_id=chain_id,
                market_id=market_id,
                positions=positions,
                complete=exhausted and not rejected,
            ),
            source=self._url("/graphql"),
            fetched_at=fetched_at,
            freshness=FreshnessStatus.FRESH,
        )

    def _paginate(
        self,
        root: str,
        query: str,
        variables: dict[str, object],
        page_size: int,
        *,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read every page; returns the raw items and whether the listing was exhausted."""
        items: list[dict[str, Any]] = []
        for _ in range(MAX_DISCOVERY_PAGES):
            page_data = self._graphql(
                query, {**variables, "first": page_size, "skip": len(items)}, root
            )
            page = page_data.get("items")
            if not isinstance(page, list) or not all(isinstance(i, dict) for i in page):
                raise _ProviderError(f"Morpho GraphQL returned no {root} items")
            items.extend(page)
            total = (page_data.get("pageInfo") or {}).get("countTotal")
            exhausted = (
                len(page) < page_size
                or (isinstance(total, int) and len(items) >= total)
            )
            if limit is not None and len(items) >= limit:
                return items[:limit], exhausted and len(items) <= limit
            if exhausted:
                return items, True
        raise _ProviderError(f"Morpho {root} pagination exceeded {MAX_DISCOVERY_PAGES} pages")

    def _get(self, path: str) -> dict[str, Any]:
        body = self._request("GET", path)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise _ProviderError(f"Morpho REST response for {path} has no data")
        return data

    def _graphql(
        self, query: str, variables: dict[str, object], root: str
    ) -> dict[str, Any]:
        body = self._request("POST", "/graphql", {"query": query, "variables": variables})
        errors = body.get("errors") or ()
        if any(
            (error.get("status") or "").upper() == "NOT_FOUND" for error in errors
        ):
            raise MorphoNotFoundError(f"Morpho GraphQL could not resolve {root}")
        if errors:
            raise _ProviderError(f"Morpho GraphQL returned errors for {root}")
        data = (body.get("data") or {}).get(root)
        if data is None:
            raise MorphoNotFoundError(f"Morpho GraphQL could not resolve {root}")
        return data

    def _request(
        self, method: str, path: str, json_body: object | None = None
    ) -> dict[str, Any]:
        failure = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http.request(
                    method, self._url(path), json=json_body, timeout=self._timeout
                )
            except httpx.TransportError as error:
                failure = type(error).__name__
            else:
                if response.status_code == 404:
                    raise MorphoNotFoundError(f"Morpho could not resolve {path}")
                if response.status_code == 429 or response.status_code >= 500:
                    failure = f"HTTP {response.status_code}"
                elif response.is_error:
                    raise _ProviderError(f"HTTP {response.status_code} for {path}")
                else:
                    return _decode(response.content)
            if attempt < self._max_attempts:
                self._sleep(self._backoff_seconds * 2 ** (attempt - 1))
        raise _ProviderError(
            f"Morpho request failed after {self._max_attempts} attempts: {failure}"
        )

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"


def _v2_allocation_rows(
    adapters: list[dict[str, Any]], decimals: int
) -> tuple[
    list[tuple[str | None, str | None, Decimal, Decimal | None, bool, int]], int
]:
    rows = []
    cap_ids: set[str] = set()
    adapter_addresses: set[str] = set()
    provider_total_raw = 0
    for adapter in adapters:
        address = adapter["adapter_address"]
        address_key = address.lower()
        if address_key in adapter_addresses:
            raise _DataConflictError(f"duplicate V2 adapter {address}")
        adapter_addresses.add(address_key)
        pending_removal = (adapter.get("pending") or {}).get("removal") is not None
        caps = adapter.get("caps") or ()
        if not isinstance(caps, list):
            raise _ProviderError(f"adapter {address} caps must be a list")
        for cap in caps:
            cap_id = str(cap["cap_id"]).lower()
            if cap_id in cap_ids:
                raise _DataConflictError(f"duplicate V2 cap {cap_id}")
            cap_ids.add(cap_id)
        adapter_caps = [cap for cap in caps if cap.get("cap_type") == "adapter"]
        if len(adapter_caps) != 1:
            raise _DataConflictError(
                f"adapter {address} must have exactly one adapter-level cap"
            )
        adapter_cap = adapter_caps[0]
        adapter_assets_raw = _nonnegative_integer(
            adapter_cap.get("real_assets")
            if adapter_cap.get("real_assets") is not None
            else adapter_cap["allocated_assets"]
        )
        provider_total_raw += adapter_assets_raw
        if adapter.get("adapter_kind") == "morpho_market_v1_v2":
            market_caps = [cap for cap in caps if cap.get("cap_type") == "market_v1"]
            market_ids: set[str] = set()
            selected_raw = 0
            for cap in market_caps:
                market_id = str(cap["market_id"])
                market_key = market_id.lower()
                if market_key in market_ids:
                    raise _DataConflictError(f"duplicate V2 market cap {market_id}")
                market_ids.add(market_key)
                assets_raw = _nonnegative_integer(
                    cap.get("expected_supply_assets")
                    if cap.get("expected_supply_assets") is not None
                    else cap["allocated_assets"]
                )
                selected_raw += assets_raw
                rows.append(
                    (
                        market_id,
                        address,
                        _units(assets_raw, decimals),
                        _optional_units(cap.get("absolute_cap"), decimals),
                        pending_removal,
                        assets_raw,
                    )
                )
            if abs(selected_raw - adapter_assets_raw) > 1:
                raise _DataConflictError(
                    f"V2 market caps for adapter {address} are incomplete"
                )
            continue
        # Unknown adapter kinds stay visible as adapter-level exposure.
        rows.append(
            (
                None,
                address,
                _units(adapter_assets_raw, decimals),
                _optional_units(adapter_cap.get("absolute_cap"), decimals),
                pending_removal,
                adapter_assets_raw,
            )
        )
    selected_total_raw = sum((row[5] for row in rows), 0)
    if abs(selected_total_raw - provider_total_raw) > 1:
        raise _DataConflictError(
            "selected V2 allocations do not match adapter-level provider totals"
        )
    return rows, provider_total_raw


def _v1_allocation_rows(
    allocations: list[dict[str, Any]], decimals: int
) -> list[tuple[str, None, Decimal, Decimal | None, bool, int]]:
    market_ids: set[str] = set()
    queue_indices: set[int] = set()
    rows = []
    for item in allocations:
        market_id = str(item["market_id"])
        market_key = market_id.lower()
        if market_key in market_ids:
            raise _DataConflictError(f"duplicate V1 market allocation {market_id}")
        market_ids.add(market_key)
        queue_index = int(item["withdraw_queue_index"])
        if queue_index in queue_indices:
            raise _DataConflictError(f"duplicate V1 withdraw queue index {queue_index}")
        queue_indices.add(queue_index)
        assets_raw = _nonnegative_integer(item["supply_assets"])
        rows.append(
            (
                market_id,
                None,
                _units(assets_raw, decimals),
                _optional_units(item.get("supply_cap"), decimals),
                (item.get("pending") or {}).get("removal") is not None,
                assets_raw,
            )
        )
    if queue_indices != set(range(len(allocations))):
        raise _DataConflictError("V1 withdraw queue is incomplete")
    return rows


_ITEM_ERRORS = (
    _ProviderError,
    AttributeError,
    DecimalException,
    KeyError,
    OverflowError,
    TypeError,
    ValueError,
    ValidationError,
)


def _parse_items(
    raw_items: Sequence[dict[str, Any]],
    parse: Callable[[dict[str, Any]], ModelT],
    identify: Callable[[dict[str, Any]], str],
) -> tuple[tuple[ModelT, ...], tuple[RejectedItem, ...]]:
    """Parse items independently so one malformed entity never aborts the batch."""
    parsed: list[ModelT] = []
    rejected: list[RejectedItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        identifier = identify(raw)
        if identifier.lower() in seen:
            continue
        seen.add(identifier.lower())
        try:
            parsed.append(parse(raw))
        except _ITEM_ERRORS as error:
            rejected.append(RejectedItem(identifier=identifier, reason=type(error).__name__))
    return tuple(parsed), tuple(rejected)


def _discovered_asset(raw: dict[str, Any]) -> DiscoveredAsset:
    return DiscoveredAsset(
        address=raw["address"],
        symbol=str(raw["symbol"]),
        decimals=int(raw["decimals"]),
        price_usd=_optional_decimal((raw.get("price") or {}).get("usd")),
    )


def _discovered_market(raw: dict[str, Any], chain_id: int) -> DiscoveredMarket:
    loan = _discovered_asset(raw["loanAsset"])
    collateral = (
        _discovered_asset(raw["collateralAsset"]) if raw.get("collateralAsset") else None
    )
    state = raw.get("state")
    if not isinstance(state, dict):
        raise _ProviderError("market state is missing")
    supply = _units(state["supplyAssets"], loan.decimals)
    borrow = _units(state["borrowAssets"], loan.decimals)
    if borrow > supply:
        raise _DataConflictError("market borrow assets exceed supply assets")
    oracle = raw.get("oracle") or {}
    bad_debt = raw.get("badDebt") or {}
    price = state.get("price")
    return DiscoveredMarket(
        chain_id=chain_id,
        market_id=raw["marketId"],
        listed=raw.get("listed"),
        lltv=_units(raw["lltv"], WAD_DECIMALS),
        loan_asset=loan,
        collateral_asset=collateral,
        oracle_address=_nonzero_address(oracle.get("address")),
        oracle_type=oracle.get("type"),
        irm_address=_nonzero_address(raw.get("irmAddress")),
        supply_assets=supply,
        borrow_assets=borrow,
        liquidity_assets=(
            _units(state["liquidityAssets"], loan.decimals)
            if state.get("liquidityAssets") is not None
            else supply - borrow
        ),
        collateral_assets=(
            _optional_units(state.get("collateralAssets"), collateral.decimals)
            if collateral is not None
            else None
        ),
        supply_usd=_optional_decimal(state.get("supplyAssetsUsd")),
        borrow_usd=_optional_decimal(state.get("borrowAssetsUsd")),
        liquidity_usd=_optional_decimal(state.get("liquidityAssetsUsd")),
        collateral_usd=_optional_decimal(state.get("collateralAssetsUsd")),
        # Derived from exact integer units instead of the provider's float ratio.
        utilization=borrow / supply if supply else None,
        bad_debt_assets=_optional_units(bad_debt.get("underlying"), loan.decimals),
        bad_debt_usd=_optional_decimal(bad_debt.get("usd")),
        # Morpho oracle prices are scaled by 1e(36 + loan decimals - collateral decimals).
        oracle_price=(
            Decimal(_nonnegative_integer(price)).scaleb(
                collateral.decimals - loan.decimals - 36
            )
            if collateral is not None and price is not None
            else None
        ),
        state_block_number=_optional_integer(state.get("blockNumber")),
        state_timestamp=_optional_integer(state.get("timestamp")),
    )


def _discovered_vault_v1(raw: dict[str, Any], chain_id: int) -> DiscoveredVault:
    asset = _discovered_asset(raw["asset"])
    state = raw.get("state")
    if not isinstance(state, dict):
        raise _ProviderError("vault state is missing")
    total = _units(state["totalAssets"], asset.decimals)
    allocations = tuple(
        DiscoveredAllocation(
            market_id=item["market"]["marketId"],
            supply_assets=_units(item["supplyAssets"], asset.decimals),
            supply_usd=_optional_decimal(item.get("supplyAssetsUsd")),
            supply_cap=_optional_units(item.get("supplyCap"), asset.decimals),
        )
        for item in state.get("allocation") or ()
    )
    liquidity = raw.get("liquidity") or {}
    allocated = sum((allocation.supply_assets for allocation in allocations), Decimal(0))
    return DiscoveredVault(
        chain_id=chain_id,
        address=raw["address"],
        version="v1",
        name=str(raw["name"]),
        symbol=str(raw["symbol"]),
        listed=raw.get("listed"),
        asset=asset,
        curator=_nonzero_address(state.get("curator")),
        total_assets=total,
        total_assets_usd=_optional_decimal(state.get("totalAssetsUsd")),
        liquidity_assets=_optional_units(liquidity.get("underlying"), asset.decimals),
        liquidity_usd=_optional_decimal(liquidity.get("usd")),
        idle_assets=None,
        allocations=allocations,
        unmodelled_assets=max(total - allocated, Decimal(0)),
        state_timestamp=_optional_integer(state.get("timestamp")),
    )


def _discovered_vault_v2(raw: dict[str, Any], chain_id: int) -> DiscoveredVault:
    asset = _discovered_asset(raw["asset"])
    decimals = asset.decimals
    total = _units(raw["totalAssets"], decimals)
    idle = _optional_units(raw.get("idleAssets"), decimals)
    allocations: list[DiscoveredAllocation] = []
    for adapter in (raw.get("adapters") or {}).get("items") or ():
        adapter_address = adapter["address"]
        if "positions" in adapter:
            for position in (adapter["positions"] or {}).get("items") or ():
                allocations.append(
                    DiscoveredAllocation(
                        market_id=position["market"]["marketId"],
                        adapter_address=adapter_address,
                        supply_assets=_units(position["state"]["supplyAssets"], decimals),
                        supply_usd=_optional_decimal(position["state"].get("supplyAssetsUsd")),
                    )
                )
        elif adapter.get("metaMorpho"):
            allocations.append(
                DiscoveredAllocation(
                    market_id=None,
                    vault_address=adapter["metaMorpho"]["address"],
                    adapter_address=adapter_address,
                    supply_assets=_units(adapter["assets"], decimals),
                )
            )
    modelled = sum((allocation.supply_assets for allocation in allocations), Decimal(0))
    # Truncated or unknown adapters stay visible as unmodelled exposure.
    unmodelled = total - (idle or Decimal(0)) - modelled
    return DiscoveredVault(
        chain_id=chain_id,
        address=raw["address"],
        version="v2",
        name=str(raw["name"]),
        symbol=str(raw["symbol"]),
        listed=raw.get("listed"),
        asset=asset,
        curator=_nonzero_address((raw.get("curator") or {}).get("address")),
        total_assets=total,
        total_assets_usd=_optional_decimal(raw.get("totalAssetsUsd")),
        liquidity_assets=_optional_units(raw.get("liquidity"), decimals),
        liquidity_usd=_optional_decimal(raw.get("liquidityUsd")),
        idle_assets=idle,
        allocations=tuple(allocations),
        unmodelled_assets=max(unmodelled, Decimal(0)),
        state_timestamp=None,
    )


def _market_position(raw: dict[str, Any], market_id: str) -> MarketPosition:
    market = raw["market"]
    if str(market["marketId"]).lower() != market_id.lower():
        raise _DataConflictError("position belongs to another market")
    loan_decimals = int(market["loanAsset"]["decimals"])
    collateral = market.get("collateralAsset")
    state = raw["state"]
    return MarketPosition(
        user=raw["user"]["address"],
        supply_assets=_units(state["supplyAssets"], loan_decimals),
        borrow_assets=_units(state["borrowAssets"], loan_decimals),
        collateral_assets=(
            _units(state["collateral"], int(collateral["decimals"]))
            if collateral
            else Decimal(0)
        ),
        health_factor=_optional_decimal(raw.get("healthFactor")),
    )


def _nonzero_address(value: object | None) -> str | None:
    if value is None or str(value).lower() == ZERO_ADDRESS:
        return None
    return str(value)


def _optional_integer(value: object | None) -> int | None:
    return None if value is None else _nonnegative_integer(value)


def _subset_key(identifiers: Sequence[str] | None) -> str:
    if identifiers is None:
        return "all"
    canonical = ",".join(sorted({identifier.lower() for identifier in identifiers}))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _vault_path(address: str, version: VaultVersion, chain_id: int) -> str:
    if version not in ("v1", "v2"):
        raise ValueError(f"unsupported vault version: {version}")
    return f"/v0/vaults-{version}/{chain_id}:{address}"


def _require_identity(
    payload: dict[str, Any], field: str, expected: str, chain_id: int
) -> None:
    if payload.get("chain_id") != chain_id:
        raise _DataConflictError(f"unexpected chain_id {payload.get('chain_id')}")
    if str(payload.get(field, "")).lower() != expected.lower():
        raise _DataConflictError(f"{field} does not match the requested entity")


def _asset(
    payload: dict[str, Any],
    *,
    expected: str | None = None,
    price_source: dict[str, Any] | None = None,
) -> AssetInfo:
    if expected is not None and payload["address"].lower() != expected.lower():
        raise _DataConflictError("asset address conflict between REST and GraphQL")
    if price_source is not None and (
        price_source["address"].lower() != payload["address"].lower()
        or int(price_source["decimals"]) != int(payload["decimals"])
    ):
        raise _DataConflictError("asset metadata conflict between REST and GraphQL")
    priced = price_source if price_source is not None else payload
    return AssetInfo(
        address=payload["address"],
        symbol=payload["symbol"],
        decimals=int(payload["decimals"]),
        price_usd=_optional_decimal((priced.get("price") or {}).get("usd")),
    )


def _units(raw: object, decimals: int) -> Decimal:
    integer = _nonnegative_integer(raw)
    # String construction is exact regardless of the active decimal precision.
    return Decimal(f"{integer}E-{decimals}")


def _optional_units(raw: object | None, decimals: int) -> Decimal | None:
    return None if raw is None else _units(raw, decimals)


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, (Decimal, int, str)) or isinstance(value, bool):
        raise _ProviderError(f"unexpected numeric value type {type(value).__name__}")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(value)
    except DecimalException as error:
        raise _ProviderError("invalid decimal value") from error
    if not decimal_value.is_finite() or decimal_value < 0:
        raise _ProviderError("decimal values must be finite and non-negative")
    return decimal_value


def _nonnegative_integer(raw: object) -> int:
    integer = int(str(raw))
    if integer < 0:
        raise ValueError("token amounts must be non-negative")
    return integer


def _derived_usd(amount: Decimal, price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    with localcontext() as context:
        context.prec = 60
        return amount * price


def _decode(content: bytes) -> dict[str, Any]:
    try:
        body = json.loads(content, parse_float=Decimal)
    except ValueError as error:
        raise _ProviderError("Morpho returned invalid JSON") from error
    if not isinstance(body, dict):
        raise _ProviderError("Morpho returned an unexpected JSON document")
    return body


def _market_key(market_id: str, chain_id: int) -> str:
    return f"market:{chain_id}:{market_id.lower()}"


def _vault_key(address: str, version: VaultVersion, chain_id: int) -> str:
    return f"vault_{version}:{chain_id}:{address.lower()}"


def _restore(
    envelope_type: type[ModelT], payload: bytes, freshness: Freshness
) -> ModelT:
    envelope = envelope_type.model_validate_json(payload)
    return envelope.model_copy(update={"freshness": FreshnessStatus(freshness.value)})


def _public_base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Morpho base URL must be an absolute HTTP(S) URL")
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
