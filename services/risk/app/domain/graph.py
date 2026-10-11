"""Inputs and identifiers for the position dependency graph.

Node identifiers are `<node_type>:<chain_id>:<address_or_key>` with the key in
lowercase, so the same on-chain object always maps to the same node regardless
of the hex casing a provider used.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.contracts.models import CatalogEntry, DecimalValue, HexIdentifier, RiskNodeType
from app.domain.snapshots import (
    AllocationSnapshot,
    DataEnvelope,
    MarketSnapshot,
    PoolSnapshot,
    VaultSnapshot,
)


if TYPE_CHECKING:
    from app.catalog.assets import AssetRegistry


CHAIN_ID = 1
ETHEREUM_KEY = "ethereum"
MORPHO_KEY = "morpho"
# Provenance for data that comes from the user or from the version-controlled catalog.
POSITION_PROVENANCE = "user input"
CATALOG_PROVENANCE = "risk0 curated catalog"


class GraphConstructionError(ValueError):
    """The graph context is inconsistent with the validated position."""


class OracleObservation(BaseModel):
    """Live oracle health read by the scenario engine (`age_seconds`, `deviation`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    oracle_address: HexIdentifier
    age_seconds: DecimalValue | None = None
    deviation: DecimalValue | None = None


@dataclass(frozen=True)
class GraphContext:
    """Everything the builder may use. Missing optional data becomes a completeness warning.

    - `market`: the entry's market (market entries).
    - `vault` and `allocations`: the entry's vault and its current allocations (vault entries).
    - `allocation_markets`: snapshots of allocated markets keyed by market id, so the vault
      graph can expand each allocation into its loan asset, collateral, and oracle.
    - `oracles`: live oracle observations keyed by oracle address.
    - `exit_pools`: candidate Uniswap exit pools.
    - `pool_depths`: executable depth per pool address, in the position numeraire,
      computed by the exit-cost layer; a pool without one reports an unknown depth.
    - `asset_registry`: issuer and underlying registry used to look through each asset
      to what backs it. `None` disables look-through (and its warnings) entirely; the
      analysis service always supplies the version-controlled registry.
    """

    entry: CatalogEntry
    market: DataEnvelope[MarketSnapshot] | None = None
    vault: DataEnvelope[VaultSnapshot] | None = None
    allocations: DataEnvelope[tuple[AllocationSnapshot, ...]] | None = None
    exit_pools: tuple[DataEnvelope[PoolSnapshot], ...] = ()
    allocation_markets: Mapping[str, DataEnvelope[MarketSnapshot]] = field(default_factory=dict)
    oracles: Mapping[str, DataEnvelope[OracleObservation]] = field(default_factory=dict)
    pool_depths: Mapping[str, Decimal] = field(default_factory=dict)
    asset_registry: "AssetRegistry | None" = None


def node_id(node_type: RiskNodeType, key: str, chain_id: int = CHAIN_ID) -> str:
    cleaned = key.strip().lower()
    if not cleaned:
        raise ValueError("node key must not be empty")
    return f"{node_type.value}:{chain_id}:{cleaned}"
