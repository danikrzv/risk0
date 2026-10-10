from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import DecimalValue, FreshnessStatus, HexIdentifier


T = TypeVar("T")
VaultVersion = Literal["v1", "v2"]


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataEnvelope(SnapshotModel, Generic[T]):
    value: T
    source: str = Field(min_length=1)
    fetched_at: datetime
    freshness: FreshnessStatus
    block_number: int | None = Field(default=None, ge=0)


class AssetInfo(SnapshotModel):
    address: HexIdentifier
    symbol: str
    decimals: int = Field(ge=0, le=36)
    price_usd: DecimalValue | None = None


class MarketSnapshot(SnapshotModel):
    chain_id: int
    market_id: HexIdentifier
    loan_asset: AssetInfo
    collateral_asset: AssetInfo
    oracle_address: HexIdentifier
    irm_address: HexIdentifier
    lltv: DecimalValue
    total_supply_assets: DecimalValue
    total_borrow_assets: DecimalValue
    liquidity_assets: DecimalValue
    utilization: DecimalValue | None
    total_supply_usd: DecimalValue | None
    total_borrow_usd: DecimalValue | None
    liquidity_usd: DecimalValue | None
    listed: bool | None
    last_accrual_timestamp: int | None
    sources: tuple[str, ...]


class VaultSnapshot(SnapshotModel):
    chain_id: int
    address: HexIdentifier
    version: VaultVersion
    name: str
    symbol: str
    asset: AssetInfo
    total_assets: DecimalValue
    liquidity_assets: DecimalValue
    idle_assets: DecimalValue | None
    share_price: DecimalValue
    total_assets_usd: DecimalValue | None
    liquidity_usd: DecimalValue | None
    listed: bool | None
    sources: tuple[str, ...]


class AllocationSnapshot(SnapshotModel):
    vault_address: HexIdentifier
    market_id: HexIdentifier | None
    adapter_address: HexIdentifier | None
    supply_assets: DecimalValue
    supply_cap: DecimalValue | None
    weight: DecimalValue
    pending_removal: bool


class PoolSnapshot(SnapshotModel):
    chain_id: int
    pool_address: HexIdentifier
    token0: AssetInfo
    token1: AssetInfo
    fee_tier: int = Field(ge=0)
    liquidity: DecimalValue
    sqrt_price_x96: DecimalValue
    sources: tuple[str, ...]
