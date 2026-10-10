import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

from app.config import CachePolicy
from app.contracts.models import FreshnessStatus
from app.domain.snapshots import (
    AllocationSnapshot,
    AssetInfo,
    DataEnvelope,
    MarketSnapshot,
    VaultSnapshot,
    VaultVersion,
)
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


DEFAULT_BASE_URL = "https://api.morpho.org"
CHAIN_ID = 1
CACHE_NAMESPACE = "morpho"
SNAPSHOT_POLICY = CachePolicy(ttl=timedelta(seconds=60), retention=timedelta(hours=24))
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

ModelT = TypeVar("ModelT", bound=BaseModel)


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
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
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

    def get_market(self, market_id: str) -> DataEnvelope[MarketSnapshot]:
        return self._load(
            f"market:{CHAIN_ID}:{market_id.lower()}",
            DataEnvelope[MarketSnapshot],
            lambda: self._fetch_market(market_id),
        )

    def get_vault(
        self, address: str, version: VaultVersion
    ) -> DataEnvelope[VaultSnapshot]:
        return self._load(
            f"vault_{version}:{CHAIN_ID}:{address.lower()}",
            DataEnvelope[VaultSnapshot],
            lambda: self._fetch_vault(address, version),
        )

    def get_allocations(
        self, address: str, version: VaultVersion
    ) -> DataEnvelope[tuple[AllocationSnapshot, ...]]:
        return self._load(
            f"allocations_{version}:{CHAIN_ID}:{address.lower()}",
            DataEnvelope[tuple[AllocationSnapshot, ...]],
            lambda: self._fetch_allocations(address, version),
        )

    def _load(
        self,
        key: str,
        envelope_type: type[ModelT],
        fetch: Callable[[], ModelT],
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
                SNAPSHOT_POLICY,
            )
        return envelope

    def _fetch_market(self, market_id: str) -> DataEnvelope[MarketSnapshot]:
        fetched_at = self._clock()
        selector = f"{CHAIN_ID}:{market_id}"
        config_path = f"/v0/blue/markets/{selector}"
        state_path = f"{config_path}/state"
        config = self._get(config_path)
        state = self._get(state_path)
        metadata = self._graphql(
            MARKET_QUERY, {"marketId": market_id, "chainId": CHAIN_ID}, "marketById"
        )

        for payload in (config, state):
            _require_identity(payload, "market_id", market_id)
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
                chain_id=CHAIN_ID,
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
        self, address: str, version: VaultVersion
    ) -> DataEnvelope[VaultSnapshot]:
        fetched_at = self._clock()
        config_path = _vault_path(address, version)
        state_path = (
            f"/v1/vaults-v2/{CHAIN_ID}:{address}/state"
            if version == "v2"
            else f"{config_path}/state"
        )
        config = self._get(config_path)
        state = self._get(state_path)
        if version == "v2":
            metadata = self._graphql(
                VAULT_V2_QUERY,
                {"address": address, "chainId": CHAIN_ID},
                "vaultV2ByAddress",
            )
            total_assets_usd = metadata.get("totalAssetsUsd")
            liquidity_usd = metadata.get("liquidityUsd")
        else:
            metadata = self._graphql(
                VAULT_V1_QUERY,
                {"address": address, "chainId": CHAIN_ID},
                "vaultByAddress",
            )
            total_assets_usd = (metadata.get("state") or {}).get("totalAssetsUsd")
            liquidity_usd = None

        for payload in (config, state):
            _require_identity(payload, "address", address)
        asset = _asset(config["asset"], price_source=metadata["asset"])
        decimals = asset.decimals

        return DataEnvelope[VaultSnapshot](
            value=VaultSnapshot(
                chain_id=CHAIN_ID,
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
        self, address: str, version: VaultVersion
    ) -> DataEnvelope[tuple[AllocationSnapshot, ...]]:
        fetched_at = self._clock()
        config_path = _vault_path(address, version)
        allocations_path = f"{config_path}/allocations"
        config = self._get(config_path)
        payload = self._get(allocations_path)
        _require_identity(config, "address", address)
        _require_identity(payload, "vault_address", address)
        decimals = int(config["asset"]["decimals"])

        if version == "v2":
            rows, provider_total_raw = _v2_allocation_rows(
                payload["allocations"], decimals
            )
        else:
            state = self._get(f"{config_path}/state")
            _require_identity(state, "address", address)
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


def _vault_path(address: str, version: VaultVersion) -> str:
    if version not in ("v1", "v2"):
        raise ValueError(f"unsupported vault version: {version}")
    return f"/v0/vaults-{version}/{CHAIN_ID}:{address}"


def _require_identity(payload: dict[str, Any], field: str, expected: str) -> None:
    if payload.get("chain_id") != CHAIN_ID:
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
