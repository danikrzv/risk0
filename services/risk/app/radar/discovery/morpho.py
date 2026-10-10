import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.adapters.morpho import (
    DiscoveredAsset,
    DiscoveredMarket,
    DiscoveredVault,
    MorphoClient,
    MorphoError,
)
from app.contracts.models import CatalogEntityType, CatalogEntry, FreshnessStatus, MetricValue
from app.radar.contracts import (
    RadarEntity,
    RadarEntityType,
    RadarRelationship,
    RadarRelationshipType,
)


SOURCE = "morpho:graphql"
PROTOCOL_KEY = "morpho"
_DEPENDENCY_TYPES = (
    RadarRelationshipType.LENDS,
    RadarRelationshipType.USES_COLLATERAL,
    RadarRelationshipType.USES_ORACLE,
    RadarRelationshipType.USES_IRM,
    RadarRelationshipType.DEPLOYED_ON,
)


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    chain_id: int | None
    kind: str
    source: str
    fetched_at: datetime
    freshness: FreshnessStatus


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    chain_id: int | None
    kind: str
    message: str
    entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    discovered_at: datetime
    chains: tuple[RadarEntity, ...]
    protocols: tuple[RadarEntity, ...]
    vaults: tuple[RadarEntity, ...]
    markets: tuple[RadarEntity, ...]
    assets: tuple[RadarEntity, ...]
    oracles: tuple[RadarEntity, ...]
    irms: tuple[RadarEntity, ...]
    relationships: tuple[RadarRelationship, ...]
    sources: tuple[DiscoverySource, ...]
    failures: tuple[DiscoveryFailure, ...]
    # Chains whose markets and vaults all loaded; only these may tombstone absences.
    complete_chain_ids: tuple[int, ...]
    market_records: tuple[DiscoveredMarket, ...] = field(repr=False)
    vault_records: tuple[DiscoveredVault, ...] = field(repr=False)

    @property
    def entities(self) -> tuple[RadarEntity, ...]:
        return (
            *self.chains,
            *self.protocols,
            *self.vaults,
            *self.markets,
            *self.assets,
            *self.oracles,
            *self.irms,
        )


def entity_id(entity_type: RadarEntityType, chain_id: int, key: str) -> str:
    return f"{entity_type.value}:{chain_id}:{key.strip().lower()}"


def chain_key(chain_id: int, network: str | None) -> str:
    # Matches the existing position graph ("chain:1:ethereum").
    slug = re.sub(r"[^a-z0-9]+", "-", (network or "").lower()).strip("-")
    return slug or str(chain_id)


class MorphoDiscovery:
    """Builds multi-chain Morpho topology, isolating failures per chain and entity."""

    def __init__(
        self,
        client: MorphoClient,
        *,
        catalog: Sequence[CatalogEntry] = (),
        chain_ids: Sequence[int] | None = None,
    ) -> None:
        self._client = client
        self._catalog = tuple(catalog)
        self._allowed = None if chain_ids is None else tuple(chain_ids)

    def discover(self, now: datetime | None = None) -> DiscoveryBatch:
        discovered_at = now or datetime.now(UTC)
        builder = _TopologyBuilder(self._catalog)
        sources: list[DiscoverySource] = []
        failures: list[DiscoveryFailure] = []
        complete: list[int] = []

        for chain_id, network in self._chains(sources, failures):
            builder.add_chain(chain_id, network)
            chain_complete = True
            markets = vaults = None
            try:
                markets = self._client.discover_markets(chain_id)
            except MorphoError as error:
                chain_complete = False
                failures.append(_failure(chain_id, "markets", error))
            try:
                vaults = self._client.discover_vaults(chain_id)
            except MorphoError as error:
                chain_complete = False
                failures.append(_failure(chain_id, "vaults", error))
            for kind, envelope in (("markets", markets), ("vaults", vaults)):
                if envelope is None:
                    continue
                sources.append(
                    DiscoverySource(
                        chain_id=chain_id,
                        kind=kind,
                        source=envelope.source,
                        fetched_at=envelope.fetched_at,
                        freshness=envelope.freshness,
                    )
                )
                entity_type = RadarEntityType.MARKET if kind == "markets" else RadarEntityType.VAULT
                for rejected in envelope.value.rejected:
                    chain_complete = False
                    failures.append(
                        DiscoveryFailure(
                            chain_id=chain_id,
                            kind=entity_type.value,
                            message=f"rejected malformed {entity_type.value}: {rejected.reason}",
                            entity_id=entity_id(entity_type, chain_id, rejected.identifier),
                        )
                    )
            for market in markets.value.items if markets is not None else ():
                builder.add_market(market)
            for vault in vaults.value.items if vaults is not None else ():
                builder.add_vault(vault)
            if chain_complete:
                complete.append(chain_id)

        return builder.build(
            discovered_at=discovered_at,
            sources=tuple(sources),
            failures=tuple(failures),
            complete_chain_ids=tuple(complete),
        )

    def _chains(
        self, sources: list[DiscoverySource], failures: list[DiscoveryFailure]
    ) -> list[tuple[int, str | None]]:
        try:
            envelope = self._client.list_chains()
        except MorphoError as error:
            failures.append(_failure(None, "chains", error))
            # Fall back to the chains we were asked for, or that the catalog needs.
            fallback = self._allowed or sorted({entry.chain_id for entry in self._catalog})
            return [(chain_id, _KNOWN_NETWORKS.get(chain_id)) for chain_id in sorted(fallback)]
        sources.append(
            DiscoverySource(
                chain_id=None,
                kind="chains",
                source=envelope.source,
                fetched_at=envelope.fetched_at,
                freshness=envelope.freshness,
            )
        )
        chains = sorted({chain.chain_id: chain.network for chain in envelope.value}.items())
        if self._allowed is not None:
            chains = [chain for chain in chains if chain[0] in self._allowed]
        return chains


# Used only when Morpho's chain listing is unavailable.
_KNOWN_NETWORKS = {1: "Ethereum", 8453: "Base"}


def _failure(chain_id: int | None, kind: str, error: Exception) -> DiscoveryFailure:
    scope = "all chains" if chain_id is None else f"chain {chain_id}"
    return DiscoveryFailure(
        chain_id=chain_id,
        kind=kind,
        message=f"Morpho {kind} discovery failed for {scope}: {type(error).__name__}",
    )


class _TopologyBuilder:
    def __init__(self, catalog: Sequence[CatalogEntry]) -> None:
        self._entities: dict[str, RadarEntity] = {}
        self._relationships: dict[tuple[str, str, str], RadarRelationship] = {}
        self._markets: list[DiscoveredMarket] = []
        self._vaults: list[DiscoveredVault] = []
        self._catalog_ids = {
            entity_id(
                RadarEntityType.MARKET
                if entry.entity_type is CatalogEntityType.MORPHO_MARKET
                else RadarEntityType.VAULT,
                entry.chain_id,
                entry.address_or_market_id,
            )
            for entry in catalog
        }

    def add_chain(self, chain_id: int, network: str | None) -> None:
        chain = self._entity(
            RadarEntityType.CHAIN,
            chain_id,
            chain_key(chain_id, network),
            network or f"Chain {chain_id}",
            {"chain_id": Decimal(chain_id)},
        )
        protocol = self._entity(
            RadarEntityType.PROTOCOL, chain_id, PROTOCOL_KEY, "Morpho", {"protocol": "morpho"}
        )
        self._edge(protocol, chain, RadarRelationshipType.DEPLOYED_ON)

    def add_market(self, market: DiscoveredMarket) -> None:
        chain_id = market.chain_id
        loan = self._asset(chain_id, market.loan_asset)
        collateral = (
            self._asset(chain_id, market.collateral_asset)
            if market.collateral_asset is not None
            else None
        )
        metadata: dict[str, MetricValue] = {
            "lltv": market.lltv,
            "listed": market.listed,
            "loan_asset": market.loan_asset.address.lower(),
            "collateral_asset": (
                market.collateral_asset.address.lower() if market.collateral_asset else None
            ),
            "oracle": market.oracle_address.lower() if market.oracle_address else None,
            "irm": market.irm_address.lower() if market.irm_address else None,
            "supply_usd": market.supply_usd,
        }
        collateral_symbol = market.collateral_asset.symbol if market.collateral_asset else "idle"
        market_id = self._entity(
            RadarEntityType.MARKET,
            chain_id,
            market.market_id,
            f"{collateral_symbol}/{market.loan_asset.symbol} "
            f"{format((market.lltv * 100).normalize(), 'f')}%",
            metadata,
        )
        self._markets.append(market)
        self._edge(market_id, loan, RadarRelationshipType.LENDS)
        if collateral is not None:
            self._edge(market_id, collateral, RadarRelationshipType.USES_COLLATERAL)
        if market.oracle_address is not None:
            oracle = self._entity(
                RadarEntityType.ORACLE,
                chain_id,
                market.oracle_address,
                f"Oracle {market.oracle_address[:10]}",
                {"oracle_type": market.oracle_type},
            )
            self._edge(market_id, oracle, RadarRelationshipType.USES_ORACLE)
        if market.irm_address is not None:
            irm = self._entity(
                RadarEntityType.IRM, chain_id, market.irm_address, f"IRM {market.irm_address[:10]}", {}
            )
            self._edge(market_id, irm, RadarRelationshipType.USES_IRM)
        self._edge(market_id, self._protocol(chain_id), RadarRelationshipType.DEPLOYED_ON)

    def add_vault(self, vault: DiscoveredVault) -> None:
        chain_id = vault.chain_id
        vault_id = self._entity(
            RadarEntityType.VAULT,
            chain_id,
            vault.address,
            vault.name,
            {
                "version": vault.version,
                "symbol": vault.symbol,
                "listed": vault.listed,
                "asset": vault.asset.address.lower(),
                "curator": vault.curator.lower() if vault.curator else None,
                "total_assets_usd": vault.total_assets_usd,
                "unmodelled_assets": vault.unmodelled_assets,
            },
        )
        self._vaults.append(vault)
        self._edge(vault_id, self._asset(chain_id, vault.asset), RadarRelationshipType.LENDS)
        self._edge(vault_id, self._protocol(chain_id), RadarRelationshipType.DEPLOYED_ON)
        for allocation in vault.allocations:
            if allocation.market_id is not None:
                target = entity_id(RadarEntityType.MARKET, chain_id, allocation.market_id)
            elif allocation.vault_address is not None:
                target = entity_id(RadarEntityType.VAULT, chain_id, allocation.vault_address)
            else:
                continue
            weight = (
                allocation.supply_assets / vault.total_assets
                if vault.total_assets
                else None
            )
            self._edge(
                vault_id,
                target,
                RadarRelationshipType.ALLOCATED_TO,
                # A weight above one means inconsistent provider data, not exposure.
                weight=weight if weight is not None and weight <= 1 else None,
                metadata={
                    "supply_assets": allocation.supply_assets,
                    "supply_usd": allocation.supply_usd,
                    "supply_cap": allocation.supply_cap,
                },
            )

    def build(
        self,
        *,
        discovered_at: datetime,
        sources: tuple[DiscoverySource, ...],
        failures: tuple[DiscoveryFailure, ...],
        complete_chain_ids: tuple[int, ...],
    ) -> DiscoveryBatch:
        # Allocation targets that failed to load would dangle; keep edges consistent.
        relationships = [
            relationship
            for relationship in self._relationships.values()
            if relationship.source_entity_id in self._entities
            and relationship.target_entity_id in self._entities
        ]
        visible = self._visible_ids(relationships)
        entities = sorted(
            (
                entity.model_copy(
                    update={
                        "catalog": entity.entity_id in self._catalog_ids,
                        "visible": entity.entity_id in visible,
                    }
                )
                for entity in self._entities.values()
            ),
            key=lambda entity: entity.entity_id,
        )

        def of(entity_type: RadarEntityType) -> tuple[RadarEntity, ...]:
            return tuple(e for e in entities if e.entity_type is entity_type)

        return DiscoveryBatch(
            discovered_at=discovered_at,
            chains=tuple(sorted(of(RadarEntityType.CHAIN), key=lambda e: e.chain_id)),
            protocols=of(RadarEntityType.PROTOCOL),
            vaults=of(RadarEntityType.VAULT),
            markets=of(RadarEntityType.MARKET),
            assets=of(RadarEntityType.ASSET),
            oracles=of(RadarEntityType.ORACLE),
            irms=of(RadarEntityType.IRM),
            relationships=tuple(
                sorted(
                    relationships,
                    key=lambda r: (
                        r.source_entity_id,
                        r.target_entity_id,
                        r.relationship_type.value,
                    ),
                )
            ),
            sources=sources,
            failures=failures,
            complete_chain_ids=complete_chain_ids,
            market_records=tuple(self._markets),
            vault_records=tuple(self._vaults),
        )

    def _visible_ids(self, relationships: Iterable[RadarRelationship]) -> set[str]:
        """Catalog entities plus everything they allocate to or structurally depend on."""
        outgoing: dict[str, list[RadarRelationship]] = {}
        for relationship in relationships:
            outgoing.setdefault(relationship.source_entity_id, []).append(relationship)
        visible = {entity for entity in self._catalog_ids if entity in self._entities}
        frontier = list(visible)
        while frontier:
            current = frontier.pop()
            for relationship in outgoing.get(current, ()):
                follows = relationship.relationship_type in _DEPENDENCY_TYPES or (
                    relationship.relationship_type is RadarRelationshipType.ALLOCATED_TO
                    and relationship.target_entity_id.startswith("market:")
                )
                if follows and relationship.target_entity_id not in visible:
                    visible.add(relationship.target_entity_id)
                    frontier.append(relationship.target_entity_id)
        return visible

    def _asset(self, chain_id: int, asset: DiscoveredAsset) -> str:
        return self._entity(
            RadarEntityType.ASSET,
            chain_id,
            asset.address,
            asset.symbol,
            {
                "symbol": asset.symbol,
                "decimals": Decimal(asset.decimals),
                "price_usd": asset.price_usd,
            },
        )

    def _protocol(self, chain_id: int) -> str:
        return entity_id(RadarEntityType.PROTOCOL, chain_id, PROTOCOL_KEY)

    def _entity(
        self,
        entity_type: RadarEntityType,
        chain_id: int,
        key: str,
        label: str,
        metadata: dict[str, MetricValue],
    ) -> str:
        identifier = entity_id(entity_type, chain_id, key)
        if identifier not in self._entities:
            self._entities[identifier] = RadarEntity(
                entity_id=identifier,
                entity_type=entity_type,
                chain_id=chain_id,
                address_or_key=key.strip().lower(),
                label=label or identifier,
                source=SOURCE,
                metadata=metadata,
            )
        return identifier

    def _edge(
        self,
        source: str,
        target: str,
        relationship_type: RadarRelationshipType,
        *,
        weight: Decimal | None = None,
        metadata: dict[str, MetricValue] | None = None,
    ) -> None:
        self._relationships[(source, target, relationship_type.value)] = RadarRelationship(
            source_entity_id=source,
            target_entity_id=target,
            relationship_type=relationship_type,
            weight=weight,
            metadata=metadata or {},
        )
