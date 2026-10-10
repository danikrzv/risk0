from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException

from pydantic import ValidationError

from app.adapters.morpho import (
    DiscoveredAsset,
    DiscoveredMarket,
    DiscoveredVault,
    MarketPositions,
    MorphoClient,
    MorphoError,
)
from app.contracts.models import FreshnessStatus, MetricValue
from app.domain.snapshots import DataEnvelope
from app.radar.collectors.common import build_snapshot, unknown_snapshot
from app.radar.contracts import RadarEntityType, StateSnapshot
from app.radar.discovery.morpho import SOURCE, DiscoveryBatch, entity_id


NEAR_LIQUIDATION_HEALTH_FACTOR = Decimal("1.10")
MARKET_FIELDS = (
    "supply_assets",
    "borrow_assets",
    "liquidity_assets",
    "collateral_assets",
    "supply_usd",
    "borrow_usd",
    "liquidity_usd",
    "collateral_usd",
    "utilization",
    "lltv",
    "bad_debt_assets",
    "bad_debt_usd",
    "oracle_price",
    "loan_price_usd",
    "collateral_price_usd",
    "listed",
    "state_block",
    "state_timestamp",
    "state_age_seconds",
)
# Fields that do not apply to a market without collateral (an idle market).
_COLLATERAL_FIELDS = frozenset(
    {"collateral_assets", "collateral_usd", "oracle_price", "collateral_price_usd"}
)
DISTRIBUTION_FIELDS = (
    "lender_count",
    "borrower_count",
    "lender_hhi",
    "borrower_hhi",
    "top_lender_share",
    "top_borrower_share",
    "near_liquidation_debt",
    "near_liquidation_share",
    "max_borrower_ltv",
    "min_health_factor",
    "borrowers_without_health_factor",
)
VAULT_FIELDS = (
    "total_assets",
    "total_assets_usd",
    "liquidity_assets",
    "liquidity_usd",
    "idle_assets",
    "allocated_assets",
    "unmodelled_assets",
    "unmodelled_share",
    "allocation_count",
    "max_allocation_share",
    "allocation_hhi",
    "asset_price_usd",
    "listed",
)
ASSET_FIELDS = ("price_usd",)
ORACLE_FIELDS = ("oracle_price", "usd_reference_price")
_ENTITY_ERRORS = (
    ArithmeticError,
    DecimalException,
    KeyError,
    TypeError,
    ValueError,
    ValidationError,
)


class MorphoStateCollector:
    """Fast-loop market, vault, asset, and oracle state, isolated per chain and entity."""

    def __init__(
        self,
        client: MorphoClient,
        *,
        near_liquidation_health_factor: Decimal = NEAR_LIQUIDATION_HEALTH_FACTOR,
    ) -> None:
        self._client = client
        self._near_liquidation = near_liquidation_health_factor

    def collect(
        self,
        batch: DiscoveryBatch,
        now: datetime,
        *,
        position_market_ids: Collection[str] | None = None,
    ) -> tuple[StateSnapshot, ...]:
        """Snapshot every batch entity; position reads default to visible markets."""
        if position_market_ids is None:
            position_market_ids = {m.entity_id for m in batch.markets if m.visible}
        markets: dict[int, list[DiscoveredMarket]] = defaultdict(list)
        vaults: dict[int, list[DiscoveredVault]] = defaultdict(list)
        for market in batch.market_records:
            markets[market.chain_id].append(market)
        for vault in batch.vault_records:
            vaults[vault.chain_id].append(vault)
        dependents: dict[int, set[str]] = defaultdict(set)
        for entity in (*batch.assets, *batch.oracles):
            dependents[entity.chain_id].add(entity.entity_id)

        snapshots: list[StateSnapshot] = []
        for chain_id in sorted(set(markets) | set(vaults) | set(dependents)):
            chain = _ChainCollection(chain_id, now, self._client, self._near_liquidation)
            chain.collect_markets(markets.get(chain_id, ()), position_market_ids)
            chain.collect_vaults(vaults.get(chain_id, ()))
            chain.fill_unknown_dependents(dependents.get(chain_id, ()))
            snapshots.extend(chain.snapshots.values())
        return tuple(sorted(snapshots, key=lambda snapshot: snapshot.entity_id))


class _ChainCollection:
    def __init__(
        self,
        chain_id: int,
        now: datetime,
        client: MorphoClient,
        near_liquidation: Decimal,
    ) -> None:
        self.chain_id = chain_id
        self.now = now
        self.client = client
        self.near_liquidation = near_liquidation
        self.source_identity = f"morpho:{chain_id}"
        self.snapshots: dict[str, StateSnapshot] = {}

    def collect_markets(
        self, records: Sequence[DiscoveredMarket], position_market_ids: Collection[str]
    ) -> None:
        if not records:
            return
        try:
            envelope = self.client.discover_markets(
                self.chain_id, market_ids=[record.market_id for record in records]
            )
        except MorphoError:
            envelope = None
        current = (
            {market.market_id.lower(): market for market in envelope.value.items}
            if envelope is not None
            else {}
        )
        oracle_markets: dict[str, list[DiscoveredMarket]] = defaultdict(list)
        for record in records:
            market_entity = entity_id(RadarEntityType.MARKET, self.chain_id, record.market_id)
            market = current.get(record.market_id.lower())
            if envelope is None or market is None:
                self._unknown(market_entity, RadarEntityType.MARKET, MARKET_FIELDS + DISTRIBUTION_FIELDS)
                continue
            try:
                self._market(market_entity, market, envelope, market_entity in position_market_ids)
                for asset in (market.loan_asset, market.collateral_asset):
                    if asset is not None:
                        self._asset(asset, envelope)
            except _ENTITY_ERRORS:
                self._unknown(market_entity, RadarEntityType.MARKET, MARKET_FIELDS + DISTRIBUTION_FIELDS)
                continue
            if market.oracle_address is not None and market.collateral_asset is not None:
                oracle_markets[market.oracle_address.lower()].append(market)
        for oracle, users in oracle_markets.items():
            oracle_entity = entity_id(RadarEntityType.ORACLE, self.chain_id, oracle)
            try:
                self._oracle(oracle_entity, sorted(users, key=lambda m: m.market_id), envelope)
            except _ENTITY_ERRORS:
                self._unknown(oracle_entity, RadarEntityType.ORACLE, ORACLE_FIELDS)

    def collect_vaults(self, records: Sequence[DiscoveredVault]) -> None:
        if not records:
            return
        try:
            envelope = self.client.discover_vaults(
                self.chain_id, addresses=[record.address for record in records]
            )
        except MorphoError:
            envelope = None
        current = (
            {vault.address.lower(): vault for vault in envelope.value.items}
            if envelope is not None
            else {}
        )
        for record in records:
            vault_entity = entity_id(RadarEntityType.VAULT, self.chain_id, record.address)
            vault = current.get(record.address.lower())
            if envelope is None or vault is None:
                self._unknown(vault_entity, RadarEntityType.VAULT, VAULT_FIELDS)
                continue
            try:
                self._vault(vault_entity, vault, envelope)
                self._asset(vault.asset, envelope)
            except _ENTITY_ERRORS:
                self._unknown(vault_entity, RadarEntityType.VAULT, VAULT_FIELDS)

    def fill_unknown_dependents(self, entity_ids: Collection[str]) -> None:
        """Assets and oracles that no fresh market or vault described become UNKNOWN."""
        for dependent in sorted(entity_ids):
            if dependent in self.snapshots:
                continue
            if dependent.startswith("asset:"):
                self._unknown(dependent, RadarEntityType.ASSET, ASSET_FIELDS)
            else:
                self._unknown(dependent, RadarEntityType.ORACLE, ORACLE_FIELDS)

    def _market(
        self,
        market_entity: str,
        market: DiscoveredMarket,
        envelope: DataEnvelope,
        with_positions: bool,
    ) -> None:
        metrics: dict[str, MetricValue] = {
            "supply_assets": market.supply_assets,
            "borrow_assets": market.borrow_assets,
            "liquidity_assets": market.liquidity_assets,
            "collateral_assets": market.collateral_assets,
            "supply_usd": market.supply_usd,
            "borrow_usd": market.borrow_usd,
            "liquidity_usd": market.liquidity_usd,
            "collateral_usd": market.collateral_usd,
            "utilization": market.utilization,
            "lltv": market.lltv,
            "bad_debt_assets": market.bad_debt_assets,
            "bad_debt_usd": market.bad_debt_usd,
            "oracle_price": market.oracle_price,
            "loan_price_usd": market.loan_asset.price_usd,
            "collateral_price_usd": (
                market.collateral_asset.price_usd if market.collateral_asset else None
            ),
            "listed": market.listed,
            "state_block": _optional_decimal(market.state_block_number),
            "state_timestamp": _optional_decimal(market.state_timestamp),
            "state_age_seconds": (
                None
                if market.state_timestamp is None
                else Decimal(max(_epoch(self.now) - market.state_timestamp, 0))
            ),
        }
        applicable = [
            name
            for name in MARKET_FIELDS
            if market.collateral_asset is not None or name not in _COLLATERAL_FIELDS
        ]
        missing = [name for name in applicable if metrics[name] is None]
        positions = self._positions(market) if with_positions else None
        if positions is None:
            metrics.update({name: None for name in DISTRIBUTION_FIELDS})
            metrics["positions_complete"] = False
            missing.extend(DISTRIBUTION_FIELDS)
        else:
            metrics.update(_distribution(positions, market.lltv, self.near_liquidation))
            metrics["positions_complete"] = positions.complete
        self._store(
            build_snapshot(
                market_entity,
                RadarEntityType.MARKET,
                observed_at=envelope.fetched_at,
                retrieved_at=self.now,
                source=envelope.source,
                source_identity=self.source_identity,
                freshness=envelope.freshness,
                metrics=metrics,
                missing_fields=missing,
            )
        )

    def _positions(self, market: DiscoveredMarket) -> MarketPositions | None:
        try:
            return self.client.get_market_positions(
                market.market_id, chain_id=self.chain_id
            ).value
        except MorphoError:
            return None

    def _vault(
        self, vault_entity: str, vault: DiscoveredVault, envelope: DataEnvelope
    ) -> None:
        total = vault.total_assets
        amounts = [a.supply_assets for a in vault.allocations if a.supply_assets > 0]
        shares = [amount / total for amount in amounts] if total else []
        metrics: dict[str, MetricValue] = {
            "total_assets": total,
            "total_assets_usd": vault.total_assets_usd,
            "liquidity_assets": vault.liquidity_assets,
            "liquidity_usd": vault.liquidity_usd,
            "idle_assets": vault.idle_assets,
            "allocated_assets": sum(amounts, Decimal(0)),
            "unmodelled_assets": vault.unmodelled_assets,
            "unmodelled_share": vault.unmodelled_assets / total if total else None,
            "allocation_count": Decimal(len(amounts)),
            "max_allocation_share": max(shares) if shares else None,
            "allocation_hhi": sum((share * share for share in shares), Decimal(0)) if shares else None,
            "asset_price_usd": vault.asset.price_usd,
            "listed": vault.listed,
            "version": vault.version,
        }
        # Vault V1 has no idle bucket, and an empty vault has no allocation shares.
        not_applicable = {"idle_assets"} if vault.version == "v1" else set()
        if not total:
            not_applicable |= {"unmodelled_share", "max_allocation_share", "allocation_hhi"}
        self._store(
            build_snapshot(
                vault_entity,
                RadarEntityType.VAULT,
                observed_at=envelope.fetched_at,
                retrieved_at=self.now,
                source=envelope.source,
                source_identity=self.source_identity,
                freshness=envelope.freshness,
                metrics=metrics,
                missing_fields=(
                    name
                    for name in VAULT_FIELDS
                    if metrics[name] is None and name not in not_applicable
                ),
            )
        )

    def _asset(self, asset: DiscoveredAsset, envelope: DataEnvelope) -> None:
        asset_entity = entity_id(RadarEntityType.ASSET, self.chain_id, asset.address)
        if asset_entity in self.snapshots:
            return
        self._store(
            build_snapshot(
                asset_entity,
                RadarEntityType.ASSET,
                observed_at=envelope.fetched_at,
                retrieved_at=self.now,
                source=envelope.source,
                source_identity=self.source_identity,
                freshness=envelope.freshness,
                metrics={
                    "price_usd": asset.price_usd,
                    "symbol": asset.symbol,
                    "decimals": Decimal(asset.decimals),
                },
                missing_fields=() if asset.price_usd is not None else ("price_usd",),
            )
        )

    def _oracle(
        self, oracle_entity: str, markets: Sequence[DiscoveredMarket], envelope: DataEnvelope
    ) -> None:
        market = markets[0]
        loan, collateral = market.loan_asset, market.collateral_asset
        reference = (
            collateral.price_usd / loan.price_usd
            if collateral.price_usd is not None and loan.price_usd
            else None
        )
        metrics: dict[str, MetricValue] = {
            "oracle_price": market.oracle_price,
            # Morpho's USD prices give an independent reference for the same pair.
            "usd_reference_price": reference,
            "pair": f"{collateral.symbol}/{loan.symbol}",
            "base_asset": collateral.address.lower(),
            "quote_asset": loan.address.lower(),
            "oracle_type": market.oracle_type,
            "market_count": Decimal(len(markets)),
        }
        self._store(
            build_snapshot(
                oracle_entity,
                RadarEntityType.ORACLE,
                observed_at=envelope.fetched_at,
                retrieved_at=self.now,
                source=envelope.source,
                source_identity=self.source_identity,
                freshness=envelope.freshness,
                metrics=metrics,
                missing_fields=(name for name in ORACLE_FIELDS if metrics[name] is None),
            )
        )

    def _unknown(
        self, entity: str, state_type: RadarEntityType, fields: Sequence[str]
    ) -> None:
        self._store(
            unknown_snapshot(
                entity,
                state_type,
                now=self.now,
                source=SOURCE,
                source_identity=self.source_identity,
                fields=fields,
            )
        )

    def _store(self, snapshot: StateSnapshot) -> None:
        self.snapshots[snapshot.entity_id] = snapshot


def _distribution(
    positions: MarketPositions, lltv: Decimal, near_liquidation: Decimal
) -> dict[str, MetricValue]:
    lenders = [p.supply_assets for p in positions.positions if p.supply_assets > 0]
    borrowers = [p for p in positions.positions if p.borrow_assets > 0]
    debts = [p.borrow_assets for p in borrowers]
    total_debt = sum(debts, Decimal(0))
    health_factors = [p.health_factor for p in borrowers if p.health_factor is not None]
    near = sum(
        (
            p.borrow_assets
            for p in borrowers
            if p.health_factor is not None and p.health_factor < near_liquidation
        ),
        Decimal(0),
    )
    min_health = min(health_factors) if health_factors else None
    return {
        "lender_count": Decimal(len(lenders)),
        "borrower_count": Decimal(len(borrowers)),
        "lender_hhi": _hhi(lenders),
        "borrower_hhi": _hhi(debts),
        "top_lender_share": _top_share(lenders),
        "top_borrower_share": _top_share(debts),
        "near_liquidation_debt": near,
        "near_liquidation_share": near / total_debt if total_debt else None,
        # A position's LTV is LLTV / health factor, so the riskiest is LLTV / min HF.
        "max_borrower_ltv": lltv / min_health if min_health else None,
        "min_health_factor": min_health,
        "borrowers_without_health_factor": Decimal(len(borrowers) - len(health_factors)),
    }


def _hhi(amounts: Sequence[Decimal]) -> Decimal | None:
    total = sum(amounts, Decimal(0))
    if not total:
        return None
    return sum(((amount / total) ** 2 for amount in amounts), Decimal(0))


def _top_share(amounts: Sequence[Decimal]) -> Decimal | None:
    total = sum(amounts, Decimal(0))
    return max(amounts) / total if total else None


def _optional_decimal(value: int | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _epoch(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())
