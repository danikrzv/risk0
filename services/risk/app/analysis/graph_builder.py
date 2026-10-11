"""Build the dependency graph of a validated position from live snapshots.

Graph shape, matching the scenario engine's traversal rules:
- vault lender:   position --lends--> vault --allocated_to(weight)--> market ...
- market lender:  position --lends--> market
- borrower:       position --borrows--> market, --borrows--> loan asset,
                  --uses_collateral--> collateral asset
- market:         --lends--> loan asset, --uses_collateral--> collateral asset,
                  --uses_oracle--> oracle, --deployed_on--> Morpho
- vault:          --lends--> vault asset, --deployed_on--> Morpho
- Morpho --deployed_on--> Ethereum
- position --exits_through--> pool --exits_through--> pool tokens already in the graph
- issuer and underlying look-through (when the context carries an asset registry):
  asset --backed_by--> underlying asset (recursive, cycle-safe, at most
  `MAX_UNDERLYING_DEPTH` token hops), asset --backed_by--> issuer protocol node
  `protocol:1:issuer-<id>` (metrics `issuer_type`, `redemption`, `source_url`,
  `defiscan_slug`), on-chain issuers (protocol, wrapper) --deployed_on--> Ethereum,
  and non-token underlyings as asset nodes `asset:1:underlying-<id>` (native ETH
  --deployed_on--> Ethereum). An asset without a registry entry gets no new nodes
  and a completeness warning that its issuer is unmodelled.
- vault --allocated_to(weight)--> protocol "Unmodelled allocation" for every
  allocation row that is not a Morpho market (for example a V2 adapter), so an
  unknown share of the vault is visible with `exposure_unknown=True` instead of
  disappearing as zero exposure. The scenario engine must treat scenarios that
  touch such a node as UNAVAILABLE rather than as zero risk.

Allocation rows are aggregated per market (Morpho V2 may split one market over
several adapters): weights and supplied assets are summed before the edge is
emitted, a negative weight is skipped with a warning, a per-market weight above
1 is capped with a warning, and a total above 1 is warned about. Exit pools are
only accepted on Ethereum mainnet and only when at least one of their tokens is
an asset already in the graph.

Every node carries the provenance, freshness and `observed_at` of the envelope
that supplied it; a URL provenance is reduced to scheme, host and path so that
credentials or API keys embedded in a source never reach a published graph.
The position node is "user input" at validation time; Morpho and Ethereum come
from the curated catalog at build time. Missing data (exit pools, allocations,
allocated-market details, oracle health, pool depth, a liquidity figure that
cannot be expressed in the numeraire) is reported in `completeness_warnings`
and never substituted.

Protocol TVL (optional, DeFiLlama): the Morpho node carries `tvl_usd`,
`chain_tvl_usd`, `chain_borrowed_usd`, `chain_defi_tvl_usd`, `chain_tvl_share`,
`entity_tvl_usd` and `entity_share_of_protocol_tvl` (see `app.domain.protocol`),
plus a concentration warning when a share reaches its named threshold.

Metric keys read by `app.analysis.scenario_engine`: `symbol` on asset nodes,
`lltv` on market nodes, `available_liquidity` (position-numeraire units) on
vault and market nodes, `liquidity_depth` on pool nodes, and `age_seconds` and
`deviation` on oracle nodes.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from app.catalog.assets import (
    MAX_UNDERLYING_DEPTH,
    AssetRegistry,
    Issuer,
    IssuerType,
    RegisteredAsset,
)
from app.contracts.models import (
    CatalogEntityType,
    FreshnessStatus,
    MetricValue,
    PositionMode,
    RiskEdge,
    RiskEdgeType,
    RiskGraph,
    RiskNode,
    RiskNodeType,
)
from app.domain.graph import (
    CATALOG_PROVENANCE,
    CHAIN_ID,
    ETHEREUM_KEY,
    MORPHO_KEY,
    POSITION_PROVENANCE,
    GraphConstructionError,
    GraphContext,
    node_id,
)
from app.domain.protocol import (
    ENTITY_PROTOCOL_TVL_SHARE_WARNING,
    PROTOCOL_CHAIN_TVL_SHARE_WARNING,
    SHARE_QUANTUM,
    ProtocolTvlSnapshot,
)
from app.domain.position import (
    USD,
    ValidatedPosition,
    convert_to_numeraire,
    quantize_value,
    symbol_matches,
)
from app.domain.snapshots import (
    AllocationSnapshot,
    AssetInfo,
    DataEnvelope,
    MarketSnapshot,
    PoolSnapshot,
    VaultSnapshot,
)


ONE = Decimal(1)
ZERO = Decimal(0)
HUNDRED = Decimal(100)
PERCENT_QUANTUM = Decimal("0.01")
UNMODELLED_ALLOCATION_LABEL = "Unmodelled allocation"
UNMODELLED_ALLOCATION_PREFIX = "unmodelled-allocation-"
REGISTRY_PROVENANCE = "risk0 asset registry"
ISSUER_KEY_PREFIX = "issuer-"
UNDERLYING_KEY_PREFIX = "underlying-"
ONCHAIN_ISSUER_TYPES = frozenset({IssuerType.PROTOCOL, IssuerType.WRAPPER})


@dataclass
class _Allocation:
    """Allocation rows aggregated per target (market id or adapter)."""

    weight: Decimal = ZERO
    supply_assets: Decimal = ZERO
    supply_cap: Decimal | None = ZERO
    pending_removal: bool = False
    adapter_addresses: set[str | None] = field(default_factory=set)
    rows: int = 0

    def add(self, row: AllocationSnapshot) -> None:
        self.weight += row.weight
        self.supply_assets += row.supply_assets
        if self.supply_cap is None or row.supply_cap is None:
            self.supply_cap = None
        else:
            self.supply_cap += row.supply_cap
        self.pending_removal = self.pending_removal or row.pending_removal
        self.adapter_addresses.add(row.adapter_address.lower() if row.adapter_address else None)
        self.rows += 1

    @property
    def adapter_address(self) -> str | None:
        """The single adapter behind the rows, or None when there are several (or none)."""
        if len(self.adapter_addresses) == 1:
            return next(iter(self.adapter_addresses))
        return None


def build_risk_graph(
    position: ValidatedPosition,
    context: GraphContext,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RiskGraph:
    generated_at = clock()
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("graph timestamp must be timezone-aware")
    _check_context(position, context)
    return _Builder(position, context, generated_at).build()


def _check_context(position: ValidatedPosition, context: GraphContext) -> None:
    entry = context.entry
    if position.entity_id != entry.entity_id:
        raise GraphConstructionError(
            f"position entity_id {position.entity_id!r} does not match context entry {entry.entity_id!r}"
        )
    if entry.chain_id != CHAIN_ID:
        raise GraphConstructionError("only Ethereum mainnet (chain 1) entries are supported")
    if not entry.supported:
        raise GraphConstructionError(f"catalog entry {entry.entity_id!r} is inactive; analysis is disabled")
    if position.mode not in entry.supported_modes:
        raise GraphConstructionError(
            f"mode {position.mode.value!r} is not supported by catalog entry {entry.entity_id!r}"
        )
    identifier = entry.address_or_market_id.lower()
    if entry.entity_type is CatalogEntityType.MORPHO_MARKET:
        if context.market is None:
            raise GraphConstructionError(f"market entry {entry.entity_id!r} requires a market snapshot")
        if context.vault is not None or context.allocations is not None:
            raise GraphConstructionError("vault data was supplied for a market entry")
        market = context.market.value
        if market.chain_id != CHAIN_ID or market.market_id.lower() != identifier:
            raise GraphConstructionError(
                f"market snapshot {market.market_id} does not match catalog entry {entry.entity_id!r}"
            )
        return
    if context.vault is None:
        raise GraphConstructionError(f"vault entry {entry.entity_id!r} requires a vault snapshot")
    if context.market is not None:
        raise GraphConstructionError("a market snapshot was supplied for a vault entry")
    vault = context.vault.value
    if vault.chain_id != CHAIN_ID or vault.address.lower() != identifier:
        raise GraphConstructionError(
            f"vault snapshot {vault.address} does not match catalog entry {entry.entity_id!r}"
        )


class _Builder:
    def __init__(
        self, position: ValidatedPosition, context: GraphContext, generated_at: datetime
    ) -> None:
        self.position = position
        self.context = context
        self.generated_at = generated_at
        self.numeraire = position.numeraire
        self.nodes: dict[str, RiskNode] = {}
        self.edges: dict[tuple[str, str, RiskEdgeType], RiskEdge] = {}
        self.warnings: list[str] = []
        self.oracles = {key.lower(): value for key, value in context.oracles.items()}
        self.allocation_markets = {
            key.lower(): value for key, value in context.allocation_markets.items()
        }
        self.pool_depths = {key.lower(): value for key, value in context.pool_depths.items()}
        self.reference_assets = _reference_assets(context)

    def build(self) -> RiskGraph:
        position_id = self._add_position()
        morpho_id = self._add_protocol_and_chain()
        if self.context.entry.entity_type is CatalogEntityType.MORPHO_MARKET:
            assert self.context.market is not None
            market_id = self._add_market(
                self.context.market, label=self.context.entry.name, primary=True
            )
            market = self.context.market.value
            if self.position.mode is PositionMode.LENDER:
                self._add_edge(position_id, market_id, RiskEdgeType.LENDS)
            else:
                self._add_edge(position_id, market_id, RiskEdgeType.BORROWS)
                self._add_edge(
                    position_id, node_id(RiskNodeType.ASSET, market.loan_asset.address), RiskEdgeType.BORROWS
                )
                self._add_edge(
                    position_id,
                    node_id(RiskNodeType.ASSET, market.collateral_asset.address),
                    RiskEdgeType.USES_COLLATERAL,
                )
        else:
            assert self.context.vault is not None
            vault_id = self._add_vault(self.context.vault, morpho_id)
            self._add_edge(position_id, vault_id, RiskEdgeType.LENDS)
            self._add_allocations(vault_id)
        self._add_pools(position_id)
        self._add_look_through()
        warnings = tuple(dict.fromkeys([*self.position.warnings, *self.warnings]))
        return RiskGraph(
            nodes=tuple(self.nodes.values()),
            edges=tuple(self.edges.values()),
            generated_at=self.generated_at,
            completeness_warnings=warnings,
        )

    # Nodes

    def _add_position(self) -> str:
        position = self.position
        metrics: dict[str, MetricValue] = {
            "mode": position.mode.value,
            "entity_id": position.entity_id,
            "numeraire": position.numeraire,
            "amount": position.amount,
            "collateral_amount": position.collateral_amount,
            "debt_amount": position.debt_amount,
            "ltv": position.ltv,
            "collateral_value": position.collateral_value,
            "debt_value": position.debt_value,
            "lltv": position.lltv,
        }
        return self._add_node(
            RiskNodeType.POSITION,
            f"{position.mode.value}-{position.entity_id}",
            f"{position.mode.value.capitalize()} position in {self.context.entry.name}",
            metrics,
            provenance=POSITION_PROVENANCE,
            freshness=FreshnessStatus.FRESH,
            observed_at=position.validated_at,
        )

    def _add_protocol_and_chain(self) -> str:
        metrics: dict[str, MetricValue] = {"name": "Morpho Blue", "protocol": MORPHO_KEY}
        provenance, freshness, observed_at = (
            CATALOG_PROVENANCE,
            FreshnessStatus.FRESH,
            self.generated_at,
        )
        tvl = self.context.protocol_tvl
        if tvl is not None:
            metrics.update(self._protocol_tvl_metrics(tvl.value))
            provenance = f"{CATALOG_PROVENANCE}; TVL: {public_source(tvl.source)}"
            freshness, observed_at = tvl.freshness, tvl.fetched_at
        morpho_id = self._add_node(
            RiskNodeType.PROTOCOL,
            MORPHO_KEY,
            "Morpho",
            metrics,
            provenance=provenance,
            freshness=freshness,
            observed_at=observed_at,
        )
        ethereum_id = self._add_node(
            RiskNodeType.CHAIN,
            ETHEREUM_KEY,
            "Ethereum",
            {"name": "Ethereum mainnet", "chain_id": Decimal(CHAIN_ID)},
            provenance=CATALOG_PROVENANCE,
            freshness=FreshnessStatus.FRESH,
            observed_at=self.generated_at,
        )
        self._add_edge(morpho_id, ethereum_id, RiskEdgeType.DEPLOYED_ON)
        return morpho_id

    def _protocol_tvl_metrics(self, tvl: ProtocolTvlSnapshot) -> dict[str, MetricValue]:
        """TVL concentration of the protocol on its chain and of the analysed entity in it."""
        if self.context.market is not None:
            entity_usd = self.context.market.value.total_supply_usd
        elif self.context.vault is not None:
            entity_usd = self.context.vault.value.total_assets_usd
        else:
            entity_usd = None
        gross = tvl.chain_gross_deposits_usd
        entity_share = None
        if entity_usd is not None and gross is not None and gross > ZERO:
            entity_share = (entity_usd / gross).quantize(SHARE_QUANTUM)
        share = tvl.chain_tvl_share
        if share is not None and share >= PROTOCOL_CHAIN_TVL_SHARE_WARNING:
            self._warn(
                f"TVL concentration: {tvl.label} holds {_percent(share)} of {tvl.chain} DeFi TVL "
                f"(DeFiLlama; threshold {_percent(PROTOCOL_CHAIN_TVL_SHARE_WARNING)}), so a "
                "protocol-wide impairment would also reach the collateral and exit markets "
                "this position depends on."
            )
        if entity_share is not None and entity_share >= ENTITY_PROTOCOL_TVL_SHARE_WARNING:
            self._warn(
                f"TVL concentration: {self.context.entry.name} is {_percent(entity_share)} of "
                f"{tvl.label}'s gross {tvl.chain} deposits (threshold "
                f"{_percent(ENTITY_PROTOCOL_TVL_SHARE_WARNING)})."
            )
        return {
            "tvl_usd": tvl.tvl_usd,
            "tvl_chain": tvl.chain,
            "chain_tvl_usd": tvl.chain_tvl_usd,
            "chain_borrowed_usd": tvl.chain_borrowed_usd,
            "chain_defi_tvl_usd": tvl.chain_defi_tvl_usd,
            "chain_tvl_share": share,
            "entity_tvl_usd": entity_usd,
            "entity_share_of_protocol_tvl": entity_share,
        }

    def _add_market(
        self,
        envelope: DataEnvelope[MarketSnapshot],
        *,
        label: str,
        primary: bool,
        allocation: _Allocation | None = None,
    ) -> str:
        market = envelope.value
        available = self._liquidity_in_numeraire(
            market.liquidity_assets, market.liquidity_usd, market.loan_asset
        )
        if primary and available is None:
            self._warn(
                f"Available liquidity of {label} could not be expressed in {self.numeraire}; "
                "it is reported as unknown."
            )
        metrics: dict[str, MetricValue] = {
            "market_id": market.market_id.lower(),
            "lltv": market.lltv,
            "loan_symbol": market.loan_asset.symbol,
            "collateral_symbol": market.collateral_asset.symbol,
            "oracle_address": market.oracle_address.lower(),
            "total_supply_assets": market.total_supply_assets,
            "total_borrow_assets": market.total_borrow_assets,
            "liquidity_assets": market.liquidity_assets,
            "available_liquidity": available,
            "utilization": market.utilization,
            "total_supply_usd": market.total_supply_usd,
            "total_borrow_usd": market.total_borrow_usd,
            "liquidity_usd": market.liquidity_usd,
            "listed": market.listed,
        }
        if allocation is not None:
            metrics.update(_allocation_metrics(allocation))
        market_node = self._add_envelope_node(
            RiskNodeType.MARKET, market.market_id, label, metrics, envelope
        )
        loan_id = self._add_asset(market.loan_asset, envelope)
        collateral_id = self._add_asset(market.collateral_asset, envelope)
        oracle_id = self._add_oracle(market.oracle_address, envelope)
        self._add_edge(market_node, loan_id, RiskEdgeType.LENDS)
        self._add_edge(market_node, collateral_id, RiskEdgeType.USES_COLLATERAL)
        self._add_edge(market_node, oracle_id, RiskEdgeType.USES_ORACLE)
        self._add_edge(market_node, node_id(RiskNodeType.PROTOCOL, MORPHO_KEY), RiskEdgeType.DEPLOYED_ON)
        return market_node

    def _add_vault(self, envelope: DataEnvelope[VaultSnapshot], morpho_id: str) -> str:
        vault = envelope.value
        available = self._liquidity_in_numeraire(
            vault.liquidity_assets, vault.liquidity_usd, vault.asset
        )
        if available is None:
            self._warn(
                f"Available liquidity of {vault.name} could not be expressed in {self.numeraire}; "
                "it is reported as unknown."
            )
        metrics: dict[str, MetricValue] = {
            "address": vault.address.lower(),
            "version": vault.version,
            "name": vault.name,
            "symbol": vault.symbol,
            "asset_symbol": vault.asset.symbol,
            "total_assets": vault.total_assets,
            "liquidity_assets": vault.liquidity_assets,
            "idle_assets": vault.idle_assets,
            "available_liquidity": available,
            "share_price": vault.share_price,
            "total_assets_usd": vault.total_assets_usd,
            "liquidity_usd": vault.liquidity_usd,
            "listed": vault.listed,
        }
        vault_id = self._add_envelope_node(RiskNodeType.VAULT, vault.address, vault.name, metrics, envelope)
        asset_id = self._add_asset(vault.asset, envelope)
        self._add_edge(vault_id, asset_id, RiskEdgeType.LENDS)
        self._add_edge(vault_id, morpho_id, RiskEdgeType.DEPLOYED_ON)
        return vault_id

    def _add_allocations(self, vault_id: str) -> None:
        envelope = self.context.allocations
        if envelope is None:
            self._warn(
                "Vault allocations are unavailable; no allocated_to edges were built and "
                "the underlying market exposure is unknown."
            )
            return
        assert self.context.vault is not None
        vault_address = self.context.vault.value.address.lower()
        markets: dict[str, _Allocation] = {}
        unmodelled: dict[str, _Allocation] = {}
        total = ZERO
        for index, allocation in enumerate(envelope.value):
            if allocation.vault_address.lower() != vault_address:
                raise GraphConstructionError(
                    f"allocation row {index} belongs to vault {allocation.vault_address.lower()}, "
                    f"not to {vault_address}"
                )
            if allocation.weight < ZERO:
                self._warn(
                    f"Allocation weight {allocation.weight} to "
                    f"{_allocation_target(allocation)} is negative and was ignored."
                )
                continue
            if allocation.weight == ZERO:
                continue
            total += allocation.weight
            if allocation.market_id is None:
                key = (
                    allocation.adapter_address.lower()
                    if allocation.adapter_address
                    else f"{UNMODELLED_ALLOCATION_PREFIX}{index}"
                )
                unmodelled.setdefault(key, _Allocation()).add(allocation)
            else:
                markets.setdefault(allocation.market_id.lower(), _Allocation()).add(allocation)
        if total > ONE:
            self._warn(
                f"Vault allocation weights sum to {total}, which exceeds 1; "
                "each allocated_to edge keeps its reported weight."
            )
        for market_id in sorted(markets):
            self._add_market_allocation(vault_id, market_id, markets[market_id], envelope)
        unmodelled_share = ZERO
        for key in sorted(unmodelled):
            unmodelled_share += self._add_unmodelled_allocation(vault_id, key, unmodelled[key], envelope)
        if unmodelled_share > ZERO:
            percent = (unmodelled_share * HUNDRED).quantize(PERCENT_QUANTUM)
            self._warn(
                f"{percent}% of vault allocations could not be modelled; "
                "scenario exposure for that share is unknown."
            )

    def _add_market_allocation(
        self,
        vault_id: str,
        market_id: str,
        allocation: _Allocation,
        envelope: DataEnvelope[tuple[AllocationSnapshot, ...]],
    ) -> None:
        weight = self._capped_weight(allocation.weight, f"market {market_id}")
        detail = self.allocation_markets.get(market_id)
        if detail is not None:
            if detail.value.market_id.lower() != market_id:
                raise GraphConstructionError(
                    f"allocation market snapshot {detail.value.market_id} does not match "
                    f"allocation {market_id}"
                )
            market = detail.value
            market_node = self._add_market(
                detail,
                label=f"{market.collateral_asset.symbol} collateral / {market.loan_asset.symbol} debt",
                primary=False,
                allocation=allocation,
            )
        else:
            market_node = self._add_envelope_node(
                RiskNodeType.MARKET,
                market_id,
                f"Morpho market {market_id}",
                {"market_id": market_id, **_allocation_metrics(allocation)},
                envelope,
            )
            self._add_edge(
                market_node, node_id(RiskNodeType.PROTOCOL, MORPHO_KEY), RiskEdgeType.DEPLOYED_ON
            )
            self._warn(
                f"Allocated market {market_id} has no market snapshot; "
                "its loan asset, collateral, oracle and LLTV are unknown."
            )
        self._add_edge(vault_id, market_node, RiskEdgeType.ALLOCATED_TO, weight)

    def _add_unmodelled_allocation(
        self,
        vault_id: str,
        key: str,
        allocation: _Allocation,
        envelope: DataEnvelope[tuple[AllocationSnapshot, ...]],
    ) -> Decimal:
        """Emit a visible node for an allocation that is not a Morpho market; return its share."""
        adapter = allocation.adapter_address
        weight = self._capped_weight(allocation.weight, f"adapter {adapter or key}")
        self._warn(
            f"Allocation of {allocation.supply_assets} to adapter {adapter or 'unknown'} is not "
            "a Morpho market; it is kept as an unmodelled allocation with unknown exposure."
        )
        metrics: dict[str, MetricValue] = {
            "exposure_unknown": True,
            "weight": weight,
            **_allocation_metrics(allocation),
        }
        node = self._add_envelope_node(
            RiskNodeType.PROTOCOL, key, UNMODELLED_ALLOCATION_LABEL, metrics, envelope
        )
        self._add_edge(vault_id, node, RiskEdgeType.ALLOCATED_TO, weight)
        return weight

    def _capped_weight(self, weight: Decimal, target: str) -> Decimal:
        if weight > ONE:
            self._warn(f"Allocation weight {weight} to {target} exceeds 1; it was capped at 1.")
            return ONE
        return weight

    def _add_pools(self, position_id: str) -> None:
        pools = self.context.exit_pools
        if not pools:
            self._warn(
                "No candidate Uniswap exit pool was supplied; exit liquidity depth is unknown."
            )
            return
        for envelope in sorted(pools, key=lambda item: item.value.pool_address.lower()):
            pool = envelope.value
            address = pool.pool_address.lower()
            if pool.chain_id != CHAIN_ID:
                raise GraphConstructionError(
                    f"exit pool {address} is on chain {pool.chain_id}; only Ethereum mainnet "
                    f"(chain {CHAIN_ID}) pools are supported"
                )
            token_ids = tuple(node_id(RiskNodeType.ASSET, token.address) for token in (pool.token0, pool.token1))
            if not any(token_id in self.nodes for token_id in token_ids):
                self._warn(
                    f"Exit pool {address} ({pool.token0.symbol}/{pool.token1.symbol}) trades neither "
                    "asset of this position and was ignored; it does not count towards exit depth."
                )
                continue
            depth = self.pool_depths.get(address)
            if depth is None:
                self._warn(
                    f"Exit pool {address} has no executable liquidity depth in {self.numeraire}; "
                    "it is reported as unknown."
                )
            else:
                depth = quantize_value(depth)
            metrics: dict[str, MetricValue] = {
                "pool_address": address,
                "token0_symbol": pool.token0.symbol,
                "token1_symbol": pool.token1.symbol,
                "token0_address": pool.token0.address.lower(),
                "token1_address": pool.token1.address.lower(),
                "fee_tier": Decimal(pool.fee_tier),
                "liquidity": pool.liquidity,
                "sqrt_price_x96": pool.sqrt_price_x96,
                "liquidity_depth": depth,
            }
            pool_id = self._add_envelope_node(
                RiskNodeType.POOL, pool.pool_address, _pool_label(pool), metrics, envelope
            )
            self._add_edge(position_id, pool_id, RiskEdgeType.EXITS_THROUGH)
            for asset_id in token_ids:
                if asset_id in self.nodes:
                    self._add_edge(pool_id, asset_id, RiskEdgeType.EXITS_THROUGH)

    def _add_asset(self, asset: AssetInfo, envelope: DataEnvelope) -> str:
        metrics: dict[str, MetricValue] = {
            "symbol": asset.symbol,
            "address": asset.address.lower(),
            "decimals": Decimal(asset.decimals),
            "price_usd": asset.price_usd,
        }
        return self._add_envelope_node(RiskNodeType.ASSET, asset.address, asset.symbol, metrics, envelope)

    def _add_oracle(self, address: str, market_envelope: DataEnvelope[MarketSnapshot]) -> str:
        key = address.lower()
        observation = self.oracles.get(key)
        if observation is not None:
            if observation.value.oracle_address.lower() != key:
                raise GraphConstructionError(
                    f"oracle observation for {observation.value.oracle_address.lower()} was "
                    f"supplied under oracle key {key}"
                )
            metrics: dict[str, MetricValue] = {
                "address": key,
                "age_seconds": observation.value.age_seconds,
                "deviation": observation.value.deviation,
            }
            return self._add_envelope_node(
                RiskNodeType.ORACLE, address, f"Oracle {key}", metrics, observation
            )
        self._warn(
            f"Oracle {key} has no live age or deviation observation; "
            "oracle-failure checks rely on the scenario severity alone."
        )
        return self._add_envelope_node(
            RiskNodeType.ORACLE,
            address,
            f"Oracle {key}",
            {"address": key, "age_seconds": None, "deviation": None},
            market_envelope,
        )

    # Issuer and underlying look-through

    def _add_look_through(self) -> None:
        registry = self.context.asset_registry
        if registry is None:
            return
        assets = sorted(
            identifier
            for identifier, node in self.nodes.items()
            if node.node_type is RiskNodeType.ASSET
        )
        expanded: set[str] = set()
        for asset_id in assets:
            self._look_through(registry, asset_id, (asset_id,), expanded)

    def _look_through(
        self,
        registry: AssetRegistry,
        asset_id: str,
        chain: tuple[str, ...],
        expanded: set[str],
    ) -> None:
        if asset_id in expanded:
            return
        expanded.add(asset_id)
        node = self.nodes[asset_id]
        address = node.metrics.get("address")
        entry = registry.asset(address) if isinstance(address, str) else None
        if entry is None:
            self._warn(
                f"{node.label} ({address}) is not in the asset registry; its issuer and "
                "underlying are unmodelled, not assumed safe."
            )
            return
        issuer = registry.issuer(entry.issuer_id)
        if issuer is None:
            self._warn(f"Issuer {entry.issuer_id!r} of {entry.symbol} is not in the asset registry.")
        else:
            issuer_id = self._add_node(
                RiskNodeType.PROTOCOL,
                f"{ISSUER_KEY_PREFIX}{issuer.issuer_id}",
                issuer.name,
                {
                    "name": issuer.name,
                    "issuer_type": issuer.issuer_type.value,
                    "redemption": issuer.redemption,
                    "source_url": issuer.source_urls[0],
                    "defiscan_slug": issuer.defiscan_slug,
                },
                provenance=REGISTRY_PROVENANCE,
                freshness=FreshnessStatus.FRESH,
                observed_at=self.generated_at,
            )
            self._add_edge(asset_id, issuer_id, RiskEdgeType.BACKED_BY)
            if issuer.issuer_type in ONCHAIN_ISSUER_TYPES:
                self._add_edge(
                    issuer_id, node_id(RiskNodeType.CHAIN, ETHEREUM_KEY), RiskEdgeType.DEPLOYED_ON
                )
        self._annotate_asset(asset_id, entry, issuer)
        underlying = entry.underlying
        if underlying.reference is not None:
            self._add_reference_underlying(registry, asset_id, entry, underlying.reference)
            return
        assert underlying.asset is not None
        target = node_id(RiskNodeType.ASSET, underlying.asset)
        if target in chain or self._reaches(target, asset_id):
            self._warn(
                f"Underlying of {entry.symbol} would form a cycle through {target}; "
                "that backed_by edge was not added."
            )
            return
        if len(chain) > MAX_UNDERLYING_DEPTH:
            self._warn(
                f"Underlying chain of {entry.symbol} exceeds {MAX_UNDERLYING_DEPTH} levels; "
                "deeper layers are unmodelled."
            )
            return
        if target not in self.nodes:
            registered = registry.asset(underlying.asset)
            self._add_node(
                RiskNodeType.ASSET,
                underlying.asset,
                registered.symbol if registered else underlying.asset.lower(),
                {
                    "symbol": registered.symbol if registered else None,
                    "address": underlying.asset.lower(),
                    "decimals": None,
                    "price_usd": None,
                },
                provenance=REGISTRY_PROVENANCE,
                freshness=FreshnessStatus.FRESH,
                observed_at=self.generated_at,
            )
        self._add_edge(asset_id, target, RiskEdgeType.BACKED_BY)
        self._look_through(registry, target, (*chain, target), expanded)

    def _add_reference_underlying(
        self, registry: AssetRegistry, asset_id: str, entry: RegisteredAsset, reference_id: str
    ) -> None:
        reference = registry.reference(reference_id)
        if reference is None:
            self._warn(f"Underlying {reference_id!r} of {entry.symbol} is not in the asset registry.")
            return
        target = self._add_node(
            RiskNodeType.ASSET,
            f"{UNDERLYING_KEY_PREFIX}{reference.reference_id}",
            f"{reference.symbol} ({'native' if reference.kind == 'native' else 'off-chain'})",
            {
                "symbol": reference.symbol,
                "description": reference.description,
                "underlying_kind": reference.kind,
                "onchain": reference.kind == "native",
                "source_url": reference.source_urls[0],
            },
            provenance=REGISTRY_PROVENANCE,
            freshness=FreshnessStatus.FRESH,
            observed_at=self.generated_at,
        )
        self._add_edge(asset_id, target, RiskEdgeType.BACKED_BY)
        if reference.kind == "native":
            self._add_edge(target, node_id(RiskNodeType.CHAIN, ETHEREUM_KEY), RiskEdgeType.DEPLOYED_ON)

    def _annotate_asset(
        self, asset_id: str, entry: RegisteredAsset, issuer: Issuer | None
    ) -> None:
        node = self.nodes[asset_id]
        metrics: dict[str, MetricValue] = {
            **node.metrics,
            "issuer": issuer.name if issuer else None,
            "issuer_type": issuer.issuer_type.value if issuer else None,
            "redemption": entry.redemption,
            "redemption_source_url": entry.sources.redemption[0],
            "underlying_source_url": entry.sources.underlying[0],
        }
        self.nodes[asset_id] = node.model_copy(update={"metrics": metrics})

    def _reaches(self, source: str, target: str) -> bool:
        """True when `target` is reachable from `source` over the edges built so far."""
        outgoing: dict[str, list[str]] = {}
        for edge in self.edges.values():
            outgoing.setdefault(edge.source, []).append(edge.target)
        seen = {source}
        stack = [source]
        while stack:
            current = stack.pop()
            if current == target:
                return True
            for child in outgoing.get(current, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return False

    # Helpers

    def _liquidity_in_numeraire(
        self, amount_assets: Decimal, amount_usd: Decimal | None, asset: AssetInfo
    ) -> Decimal | None:
        if symbol_matches(self.numeraire, asset.symbol):
            return quantize_value(amount_assets)
        if symbol_matches(self.numeraire, USD) and amount_usd is not None:
            return quantize_value(amount_usd)
        return convert_to_numeraire(amount_assets, asset, self.numeraire, self.reference_assets)

    def _add_envelope_node(
        self,
        node_type: RiskNodeType,
        key: str,
        label: str,
        metrics: dict[str, MetricValue],
        envelope: DataEnvelope,
    ) -> str:
        return self._add_node(
            node_type,
            key,
            label,
            metrics,
            provenance=public_source(envelope.source),
            freshness=envelope.freshness,
            observed_at=envelope.fetched_at,
        )

    def _add_node(
        self,
        node_type: RiskNodeType,
        key: str,
        label: str,
        metrics: dict[str, MetricValue],
        *,
        provenance: str,
        freshness: FreshnessStatus,
        observed_at: datetime,
    ) -> str:
        identifier = node_id(node_type, key)
        if identifier not in self.nodes:
            self.nodes[identifier] = RiskNode(
                node_id=identifier,
                node_type=node_type,
                label=label,
                metrics=metrics,
                provenance=provenance,
                freshness=freshness,
                observed_at=observed_at,
            )
        return identifier

    def _add_edge(
        self, source: str, target: str, relationship: RiskEdgeType, weight: Decimal | None = None
    ) -> None:
        if source == target:
            raise GraphConstructionError(f"self-referencing edge on {source}")
        if source not in self.nodes or target not in self.nodes:
            raise GraphConstructionError(f"edge {source} -> {target} references a missing node")
        key = (source, target, relationship)
        if key not in self.edges:
            self.edges[key] = RiskEdge(
                source=source, target=target, relationship=relationship, exposure_weight=weight
            )

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _reference_assets(context: GraphContext) -> tuple[AssetInfo, ...]:
    assets: list[AssetInfo] = []
    if context.market is not None:
        assets.extend((context.market.value.loan_asset, context.market.value.collateral_asset))
    if context.vault is not None:
        assets.append(context.vault.value.asset)
    for key in sorted(context.allocation_markets, key=str.lower):
        market = context.allocation_markets[key].value
        assets.extend((market.loan_asset, market.collateral_asset))
    return _unique_by_address(assets)


def _unique_by_address(assets: Iterable[AssetInfo]) -> tuple[AssetInfo, ...]:
    seen: dict[str, AssetInfo] = {}
    for asset in assets:
        seen.setdefault(asset.address.lower(), asset)
    return tuple(seen.values())


def _allocation_metrics(allocation: _Allocation) -> dict[str, MetricValue]:
    return {
        "allocation_weight": allocation.weight,
        "allocated_assets": allocation.supply_assets,
        "supply_cap": allocation.supply_cap,
        "pending_removal": allocation.pending_removal,
        "adapter_address": allocation.adapter_address,
        "allocation_rows": Decimal(allocation.rows),
    }


def _allocation_target(allocation: AllocationSnapshot) -> str:
    if allocation.market_id is not None:
        return f"market {allocation.market_id.lower()}"
    if allocation.adapter_address is not None:
        return f"adapter {allocation.adapter_address.lower()}"
    return "an unidentified adapter"


def public_source(source: str) -> str:
    """Reduce a URL source to scheme, host and path; anything that is not a URL is unchanged.

    Userinfo (`user:secret@`), the query string and the fragment are dropped so a
    provider URL carrying credentials or an API key never becomes node provenance.
    """
    parts = urlsplit(source)
    if not parts.scheme or not parts.netloc:
        return source
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _percent(share: Decimal) -> str:
    return f"{(share * HUNDRED).quantize(Decimal('0.1')):f}%"


def _pool_label(pool: PoolSnapshot) -> str:
    fee_percent = (Decimal(pool.fee_tier) / Decimal(10000)).normalize()
    return f"Uniswap V3 {pool.token0.symbol}/{pool.token1.symbol} {fee_percent:f}%"
