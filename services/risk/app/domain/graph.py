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
from app.domain.protocol import ProtocolTvlSnapshot
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


class OracleFeedObservation(BaseModel):
    """One price source read by a decoded oracle (`kind` is an `app.adapters.morpho_oracle.FeedKind`).

    `staleness_seconds` is the age beyond the heartbeat (zero for a feed that
    computes its answer when read); `heartbeat_source` says whether the
    heartbeat was documented, published by a third-party provider, inferred
    from round history, or is unknown. A third-party feed with an unknown
    heartbeat has an observed `last_update_age_seconds` but no
    `staleness_seconds`: its staleness is not judged. `provider` is parsed from
    the description for display only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    address: HexIdentifier
    kind: str
    description: str | None = None
    answer: DecimalValue | None = None
    last_update_age_seconds: DecimalValue | None = None
    heartbeat_seconds: DecimalValue | None = None
    heartbeat_source: str | None = None
    staleness_seconds: DecimalValue | None = None
    provider: str | None = None
    history_readable: bool | None = None

    @property
    def staleness_unjudged(self) -> bool:
        return self.kind == "third_party" and self.staleness_seconds is None


class OracleObservation(BaseModel):
    """Live oracle health read by the scenario engine (`age_seconds`, `deviation`).

    `oracle_type` and `feeds` describe the decoded oracle when it was decomposed
    into its feeds; both stay empty for an observation without decomposition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    oracle_address: HexIdentifier
    age_seconds: DecimalValue | None = None
    deviation: DecimalValue | None = None
    oracle_type: str | None = None
    feeds: tuple[OracleFeedObservation, ...] = ()

    @property
    def unjudged_feeds(self) -> tuple[OracleFeedObservation, ...]:
        """Third-party feeds whose age is observed but whose staleness is not judged."""
        return tuple(feed for feed in self.feeds if feed.staleness_unjudged)


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
    - `protocol_tvl`: Morpho's TVL and chain TVL share (DeFiLlama) for the protocol node.
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
    protocol_tvl: DataEnvelope[ProtocolTvlSnapshot] | None = None


def node_id(node_type: RiskNodeType, key: str, chain_id: int = CHAIN_ID) -> str:
    cleaned = key.strip().lower()
    if not cleaned:
        raise ValueError("node key must not be empty")
    return f"{node_type.value}:{chain_id}:{cleaned}"
