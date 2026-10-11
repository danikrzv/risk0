"""Inputs and identifiers for the position dependency graph.

Node identifiers are `<node_type>:<chain_id>:<address_or_key>` with the key in
lowercase, so the same on-chain object always maps to the same node regardless
of the hex casing a provider used.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.contracts.models import CatalogEntry, DecimalValue, HexIdentifier, RiskNodeType
from app.domain.snapshots import (
    AllocationSnapshot,
    DataEnvelope,
    MarketSnapshot,
    PoolSnapshot,
    VaultSnapshot,
)


CHAIN_ID = 1
ETHEREUM_KEY = "ethereum"
MORPHO_KEY = "morpho"
# Provenance for data that comes from the user or from the version-controlled catalog.
POSITION_PROVENANCE = "user input"
CATALOG_PROVENANCE = "risk0 curated catalog"


class GraphConstructionError(ValueError):
    """The graph context is inconsistent with the validated position."""


class OracleFeedObservation(BaseModel):
    """One price source read by a decoded oracle (a Chainlink feed or an on-chain rate adapter).

    `staleness_seconds` is the age beyond the heartbeat (zero for a rate adapter,
    which computes its answer when read); `heartbeat_source` says whether the
    heartbeat came from the documented table or was inferred from round history.
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
    """

    entry: CatalogEntry
    market: DataEnvelope[MarketSnapshot] | None = None
    vault: DataEnvelope[VaultSnapshot] | None = None
    allocations: DataEnvelope[tuple[AllocationSnapshot, ...]] | None = None
    exit_pools: tuple[DataEnvelope[PoolSnapshot], ...] = ()
    allocation_markets: Mapping[str, DataEnvelope[MarketSnapshot]] = field(default_factory=dict)
    oracles: Mapping[str, DataEnvelope[OracleObservation]] = field(default_factory=dict)
    pool_depths: Mapping[str, Decimal] = field(default_factory=dict)


def node_id(node_type: RiskNodeType, key: str, chain_id: int = CHAIN_ID) -> str:
    cleaned = key.strip().lower()
    if not cleaned:
        raise ValueError("node key must not be empty")
    return f"{node_type.value}:{chain_id}:{cleaned}"
