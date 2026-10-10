from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.contracts.models import CatalogEntry, FreshnessStatus
from app.radar.contracts import RadarEntityType
from tests.conftest import FakeClock
from tests.radar.morpho_graphql import (
    BASE_URL,
    BASE_VAULT,
    CATALOG_VAULT,
    M_BASE,
    M_WETH_USDC,
    M_WSTETH_WETH,
    ORACLE_WSTETH_WETH,
    V2_VAULT,
    FakeMorphoGraphql,
)


DISCOVERED_AT = datetime(2026, 10, 10, 11, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
WSTETH_MARKET = f"market:1:{M_WSTETH_WETH}"
WETH_USDC_MARKET = f"market:1:{M_WETH_USDC}"
BASE_MARKET = f"market:8453:{M_BASE}"
ORACLE_WSTETH_WETH_ENTITY = f"oracle:1:{ORACLE_WSTETH_WETH}"


@pytest.fixture
def api() -> FakeMorphoGraphql:
    return FakeMorphoGraphql()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(DISCOVERED_AT)


@pytest.fixture
def client(api, cache, clock):
    from app.adapters.morpho import MorphoClient

    return MorphoClient(
        BASE_URL,
        http_client=api.http_client(),
        cache=cache,
        clock=clock,
        sleep=lambda _: None,
        discovery_page_size=50,
    )


@pytest.fixture
def batch(client, clock):
    from app.radar.discovery import MorphoDiscovery

    catalog = CatalogEntry.model_validate(
        {
            "entity_id": "catalog-weth",
            "chain_id": 1,
            "entity_type": "morpho_vault_v1",
            "address_or_market_id": CATALOG_VAULT,
            "name": "Catalog WETH",
            "description": "Synthetic catalog vault",
            "provenance": "rockawayx_curated",
            "provenance_label": "Synthetic",
            "supported": True,
            "supported_modes": ["lender"],
            "lltv": None,
        }
    )
    discovered = MorphoDiscovery(client, catalog=(catalog,)).discover(DISCOVERED_AT)
    clock.now = NOW
    return discovered


def collect(client, batch, **kwargs):
    from app.radar.collectors import MorphoStateCollector

    return {
        snapshot.entity_id: snapshot
        for snapshot in MorphoStateCollector(client).collect(batch, NOW, **kwargs)
    }


def test_market_snapshot_carries_exact_state_and_provenance(client, batch):
    snapshot = collect(client, batch)[WETH_USDC_MARKET]

    assert snapshot.state_type is RadarEntityType.MARKET
    assert snapshot.freshness is FreshnessStatus.FRESH
    assert snapshot.observed_at == NOW
    assert snapshot.retrieved_at == NOW
    assert snapshot.source == f"{BASE_URL}/graphql"
    assert snapshot.source_identity == "morpho:1"
    metrics = snapshot.metrics
    assert metrics["supply_assets"] == Decimal("1000")
    assert metrics["borrow_assets"] == Decimal("800")
    assert metrics["available_liquidity"] == Decimal("200")
    assert metrics["utilization"] == Decimal("0.8")
    assert metrics["lltv"] == Decimal("0.86")
    assert metrics["oracle_price"] == Decimal("2500")
    assert metrics["bad_debt_usd"] == Decimal("0")
    assert metrics["bad_debt"] == Decimal("0")
    assert metrics["listed"] is True
    assert metrics["state_age_seconds"] == Decimal(int(NOW.timestamp()) - 1791633000)
    assert all(not isinstance(value, float) for value in metrics.values())


def test_market_distribution_metrics_are_derived_from_positions(client, batch):
    metrics = collect(client, batch)[WSTETH_MARKET].metrics

    assert metrics["positions_complete"] is True
    assert metrics["lender_count"] == Decimal(2)
    assert metrics["borrower_count"] == Decimal(2)
    assert metrics["top_lender_share"] == Decimal("0.6")
    assert metrics["lender_hhi"] == Decimal("0.52")
    assert metrics["top_borrower_share"] == Decimal("0.8")
    assert metrics["borrower_hhi"] == Decimal("0.68")
    assert metrics["near_liquidation_debt"] == Decimal("400")
    assert metrics["near_liquidation_debt_ratio"] == Decimal("0.8")
    assert metrics["max_borrower_ltv"] == Decimal("0.965") / Decimal("1.02")
    assert metrics["min_health_factor"] == Decimal("1.02")


def test_positions_are_only_fetched_for_visible_markets_by_default(client, api, batch):
    snapshots = collect(client, batch)

    requested = {
        market
        for request in api.requests_for("marketPositions")
        for market in request["variables"]["marketIds"]
    }
    assert M_WSTETH_WETH in requested
    assert M_BASE not in requested
    base = snapshots[BASE_MARKET]
    assert base.metrics["lender_count"] is None
    assert "lender_count" in base.missing_fields
    assert base.freshness is FreshnessStatus.FRESH


def test_vault_snapshot_reports_allocation_concentration_and_unmodelled_assets(
    client, batch
):
    snapshots = collect(client, batch)

    v1 = snapshots[f"vault:1:{CATALOG_VAULT}"].metrics
    assert v1["total_assets"] == Decimal("1000")
    assert v1["withdrawal_liquidity"] == Decimal("400")
    assert v1["allocation_count"] == Decimal(2)
    assert v1["max_allocation_share"] == Decimal("0.6")
    assert v1["allocation_hhi"] == Decimal("0.52")
    assert v1["unmodelled_allocation_ratio"] == Decimal(0)

    v2 = snapshots[f"vault:1:{V2_VAULT}"].metrics
    assert v2["unmodelled_assets"] == Decimal("0.5")
    assert v2["unmodelled_allocation_ratio"] == Decimal("0.5") / Decimal("3")
    assert v2["idle_assets"] == Decimal("0.5")


def test_asset_and_oracle_snapshots_are_emitted_once_per_entity(client, batch):
    snapshots = collect(client, batch)

    weth = snapshots[f"asset:1:0x{'e' * 40}"]
    assert weth.state_type is RadarEntityType.ASSET
    assert weth.metrics["price_usd"] == Decimal("2500.5")
    assert weth.metrics["symbol"] == "WETH"

    oracle = snapshots[f"oracle:1:{ORACLE_WSTETH_WETH}"]
    assert oracle.state_type is RadarEntityType.ORACLE
    assert oracle.metrics["oracle_price"] == Decimal("1.2")
    assert oracle.metrics["pair"] == "wstETH/WETH"
    # Morpho USD prices give an independent reference: 3000.25 / 2500.5.
    assert oracle.metrics["usd_reference_price"] == Decimal("3000.25") / Decimal("2500.5")
    assert oracle.metrics["market_count"] == Decimal(1)


def test_one_failing_chain_preserves_the_other_chains_snapshots(client, api, batch):
    api.failing_chains = {8453}

    snapshots = collect(client, batch)

    assert snapshots[WETH_USDC_MARKET].freshness is FreshnessStatus.FRESH
    base = snapshots[BASE_MARKET]
    assert base.freshness is FreshnessStatus.EXPIRED
    assert base.source_identity == "morpho:8453"
    assert base.metrics["supply_assets"] is None
    assert "supply_assets" in base.missing_fields
    base_vault = snapshots[f"vault:8453:{BASE_VAULT}"]
    assert base_vault.freshness is FreshnessStatus.EXPIRED
    assert base_vault.metrics["total_assets"] is None


def test_one_malformed_market_does_not_abort_the_batch(client, api, batch):
    api.world["markets"][1][0]["state"]["borrowAssets"] = "garbage"

    snapshots = collect(client, batch)

    broken = snapshots[WETH_USDC_MARKET]
    assert broken.freshness is FreshnessStatus.EXPIRED
    assert broken.metrics["borrow_assets"] is None
    assert snapshots[WSTETH_MARKET].freshness is FreshnessStatus.FRESH


def test_ids_are_fetched_in_chunks_and_a_failed_chunk_only_blanks_its_own_entities(
    client, api, batch, monkeypatch
):
    from app.radar.collectors import morpho as collector

    monkeypatch.setattr(collector, "ID_FILTER_CHUNK", 1)
    api.failing_ids = {M_WETH_USDC}

    snapshots = collect(client, batch)

    filters = [
        (request.get("variables") or {}).get("marketIds")
        or (request.get("variables") or {}).get("addresses")
        for request in api.requests
        if "marketPositions" not in request["query"]
    ]
    assert all(len(ids) <= 1 for ids in filters if ids)
    assert snapshots[WETH_USDC_MARKET].freshness is FreshnessStatus.EXPIRED
    assert snapshots[WSTETH_MARKET].freshness is FreshnessStatus.FRESH
    assert snapshots[ORACLE_WSTETH_WETH_ENTITY].freshness is FreshnessStatus.FRESH


def test_market_missing_from_the_provider_is_unknown(client, api, batch):
    api.world["markets"][1] = [
        market for market in api.world["markets"][1] if market["marketId"] != M_WETH_USDC
    ]

    snapshot = collect(client, batch)[WETH_USDC_MARKET]

    assert snapshot.freshness is FreshnessStatus.EXPIRED
    assert "state" in snapshot.missing_fields


def test_position_failure_only_blanks_distribution_metrics(client, api, batch):
    api.failing_roots = {"marketPositions"}

    snapshot = collect(client, batch)[WSTETH_MARKET]

    assert snapshot.freshness is FreshnessStatus.FRESH
    assert snapshot.metrics["supply_assets"] == Decimal("1000")
    assert snapshot.metrics["lender_count"] is None
    assert snapshot.metrics["positions_complete"] is False
    assert "lender_count" in snapshot.missing_fields
    assert "near_liquidation_debt" in snapshot.missing_fields


def test_stale_cached_state_is_labelled_with_its_original_observation_time(
    client, api, batch, clock
):
    from app.radar.collectors import MorphoStateCollector

    collector = MorphoStateCollector(client)
    collector.collect(batch, NOW)
    clock.now = NOW + timedelta(minutes=5)
    api.mode = "down"

    snapshots = {
        s.entity_id: s for s in collector.collect(batch, NOW + timedelta(minutes=5))
    }

    stale = snapshots[WETH_USDC_MARKET]
    assert stale.freshness is FreshnessStatus.STALE_USABLE
    assert stale.observed_at == NOW
    assert stale.retrieved_at == NOW + timedelta(minutes=5)


def test_discovered_topology_and_collected_state_persist_together(
    client, api, batch, tmp_path
):
    from app.radar.repositories import RadarDatabase, StateRepository, TopologyRepository

    database = RadarDatabase(tmp_path / "radar.sqlite3")
    database.migrate()
    topology = TopologyRepository(database)
    states = StateRepository(database)
    api.failing_chains = {8453}

    topology.upsert_entities(batch.entities, seen_at=DISCOVERED_AT)
    topology.upsert_relationships(batch.relationships, seen_at=DISCOVERED_AT)
    snapshots = collect(client, batch)
    appended = [states.append_snapshot(snapshot) for snapshot in snapshots.values()]

    assert all(appended)
    assert states.latest_snapshot(BASE_MARKET).freshness is FreshnessStatus.EXPIRED
    assert states.latest_snapshot(WSTETH_MARKET).metrics["lender_hhi"] == Decimal("0.52")
    # Re-collecting the same cached observation does not duplicate history.
    again = collect(client, batch)
    assert not states.append_snapshot(again[WSTETH_MARKET])


def test_optional_usd_figures_stay_null_without_making_the_market_unknown(
    client, api, batch
):
    api.world["markets"][1][1]["state"]["supplyAssetsUsd"] = None

    snapshot = collect(client, batch)[WSTETH_MARKET]

    assert snapshot.metrics["supply_usd"] is None
    assert "supply_usd" not in snapshot.missing_fields
    assert snapshot.missing_fields == ()


def test_collected_snapshots_feed_the_health_evaluator(client, api, batch):
    from app.radar.analysis.health import HealthEvaluator, HealthHistory
    from app.radar.contracts import HealthStatus

    api.failing_chains = {8453}
    snapshots = collect(client, batch)
    entities = {entity.entity_id: entity for entity in batch.entities}
    evaluator = HealthEvaluator()

    def evaluate(entity_id: str) -> HealthStatus:
        return evaluator.evaluate(
            entities[entity_id], snapshots[entity_id], (), HealthHistory()
        ).status

    assert evaluate(WSTETH_MARKET) is not HealthStatus.UNKNOWN
    assert evaluate(f"vault:1:{CATALOG_VAULT}") is not HealthStatus.UNKNOWN
    assert evaluate(BASE_MARKET) is HealthStatus.UNKNOWN
    # Markets without position data cannot be judged on concentration.
    assert evaluate(WETH_USDC_MARKET) is HealthStatus.UNKNOWN
