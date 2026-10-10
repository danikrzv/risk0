from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.contracts.models import CatalogEntry, FreshnessStatus
from app.radar.contracts import RadarEntityType, RadarRelationshipType
from tests.conftest import FakeClock
from tests.radar.morpho_graphql import (
    BASE_URL,
    BASE_VAULT,
    CATALOG_VAULT,
    IRM,
    M_BASE,
    M_IDLE_USDC,
    M_WETH_USDC,
    M_WSTETH_WETH,
    NEIGHBOR_VAULT,
    ORACLE_WETH_USDC,
    ORACLE_WSTETH_WETH,
    V2_VAULT,
    FakeMorphoGraphql,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def lower(value: str) -> str:
    return value.lower()


@pytest.fixture
def api() -> FakeMorphoGraphql:
    return FakeMorphoGraphql()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def client(api, cache, clock):
    from app.adapters.morpho import MorphoClient

    return MorphoClient(
        BASE_URL,
        http_client=api.http_client(),
        cache=cache,
        clock=clock,
        sleep=lambda _: None,
        discovery_page_size=2,
    )


def catalog_entry(**overrides) -> CatalogEntry:
    values = {
        "entity_id": "catalog-weth",
        "chain_id": 1,
        "entity_type": "morpho_vault_v1",
        "address_or_market_id": CATALOG_VAULT.upper().replace("0X", "0x"),
        "name": "Catalog WETH",
        "description": "Synthetic catalog vault",
        "provenance": "rockawayx_curated",
        "provenance_label": "Synthetic",
        "supported": True,
        "supported_modes": ["lender"],
        "lltv": None,
    }
    values.update(overrides)
    return CatalogEntry.model_validate(values)


def discovery(client, **kwargs):
    from app.radar.discovery import MorphoDiscovery

    kwargs.setdefault("catalog", (catalog_entry(),))
    return MorphoDiscovery(client, **kwargs)


# --- MorphoClient discovery methods -------------------------------------------------


def test_markets_are_paginated_and_converted_with_token_decimals(client, api):
    envelope = client.discover_markets(1)
    markets = {market.market_id: market for market in envelope.value.items}

    assert len(api.requests_for("markets")) == 2
    assert [r["variables"]["skip"] for r in api.requests_for("markets")] == [0, 2]
    assert set(markets) == {M_WETH_USDC, M_WSTETH_WETH, M_IDLE_USDC}
    weth_usdc = markets[M_WETH_USDC]
    assert weth_usdc.chain_id == 1
    assert weth_usdc.lltv == Decimal("0.86")
    assert weth_usdc.supply_assets == Decimal("1000")
    assert weth_usdc.borrow_assets == Decimal("800")
    assert weth_usdc.collateral_assets == Decimal("0.5")
    assert weth_usdc.oracle_price == Decimal("2500")
    assert weth_usdc.supply_usd == Decimal("1000.5")
    assert weth_usdc.loan_asset.price_usd == Decimal("0.9998")
    assert weth_usdc.oracle_address == ORACLE_WETH_USDC
    assert weth_usdc.irm_address == IRM
    assert weth_usdc.bad_debt_assets == Decimal(0)
    assert envelope.source == f"{BASE_URL}/graphql"
    assert envelope.fetched_at == NOW
    assert envelope.freshness is FreshnessStatus.FRESH


def test_idle_market_keeps_missing_collateral_and_oracle_as_none(client):
    idle = next(
        market
        for market in client.discover_markets(1).value.items
        if market.market_id == M_IDLE_USDC
    )

    assert idle.collateral_asset is None
    assert idle.oracle_address is None
    assert idle.irm_address is None
    assert idle.oracle_price is None


def test_vaults_combine_v1_and_v2_with_explicit_unmodelled_assets(client):
    vaults = {vault.address: vault for vault in client.discover_vaults(1).value.items}

    v1 = vaults[CATALOG_VAULT]
    assert v1.version == "v1"
    assert v1.curator == "0x8888888888888888888888888888888888888888"
    assert v1.total_assets == Decimal("1000")
    assert v1.liquidity_assets == Decimal("400")
    assert [(a.market_id, a.supply_assets) for a in v1.allocations] == [
        (M_WSTETH_WETH, Decimal("600")),
        (M_IDLE_USDC, Decimal("400")),
    ]
    assert v1.unmodelled_assets == Decimal(0)

    v2 = vaults[V2_VAULT]
    assert v2.version == "v2"
    assert v2.total_assets == Decimal("3")
    assert v2.idle_assets == Decimal("0.5")
    assert [(a.market_id, a.vault_address, a.supply_assets) for a in v2.allocations] == [
        (M_WETH_USDC, None, Decimal("1.5")),
        (None, NEIGHBOR_VAULT, Decimal("0.5")),
    ]
    # 2.0 sits in the market adapter but only 1.5 is attributed to a market.
    assert v2.unmodelled_assets == Decimal("0.5")


def test_discovery_cache_keys_include_the_chain_id(client, api, cache):
    client.discover_markets(1)
    client.discover_markets(8453)
    requests = len(api.requests)

    again = client.discover_markets(1)

    assert len(api.requests) == requests
    assert {m.chain_id for m in again.value.items} == {1}
    assert cache.get("morpho", "discovery_markets:1:all", NOW) is not None
    assert cache.get("morpho", "discovery_markets:8453:all", NOW) is not None


def test_subset_reads_use_distinct_cache_keys_and_filters(client, api):
    subset = client.discover_markets(1, market_ids=(M_WETH_USDC,))

    assert [m.market_id for m in subset.value.items] == [M_WETH_USDC]
    assert api.requests_for("markets")[-1]["variables"]["marketIds"] == [M_WETH_USDC]
    assert len(client.discover_markets(1).value.items) == 3


def test_expired_cache_is_not_served_when_the_provider_fails(client, api, clock):
    from app.adapters.morpho import MorphoUnavailableError

    client.discover_markets(1)
    clock.advance(timedelta(minutes=45))
    api.mode = "down"

    stale = client.discover_markets(1)
    assert stale.freshness is FreshnessStatus.STALE_USABLE
    assert stale.fetched_at == NOW

    clock.advance(timedelta(days=2))
    with pytest.raises(MorphoUnavailableError):
        client.discover_markets(1)


def test_malformed_items_are_rejected_individually(client, api):
    api.world["markets"][1][0]["lltv"] = "not-a-number"

    envelope = client.discover_markets(1)

    assert {m.market_id for m in envelope.value.items} == {M_WSTETH_WETH, M_IDLE_USDC}
    assert [r.identifier for r in envelope.value.rejected] == [M_WETH_USDC]


def test_chains_are_listed(client):
    chains = client.list_chains().value

    assert [(c.chain_id, c.network) for c in chains] == [(1, "Ethereum"), (8453, "Base")]


def test_market_positions_are_paginated_and_exact(client, api):
    envelope = client.get_market_positions(M_WSTETH_WETH, chain_id=1)

    positions = envelope.value.positions
    assert envelope.value.complete is True
    assert len(api.requests_for("marketPositions")) == 2
    assert [p.supply_assets for p in positions[:2]] == [Decimal("600"), Decimal("400")]
    assert positions[2].borrow_assets == Decimal("400")
    assert positions[2].collateral_assets == Decimal("350")
    assert positions[2].health_factor == Decimal("1.02")
    assert positions[0].health_factor is None


def test_market_positions_stop_at_the_cap_and_report_incompleteness(api, cache, clock):
    from app.adapters.morpho import MorphoClient

    capped = MorphoClient(
        BASE_URL,
        http_client=api.http_client(),
        cache=cache,
        clock=clock,
        sleep=lambda _: None,
        discovery_page_size=2,
        max_positions_per_market=3,
    )

    envelope = capped.get_market_positions(M_WSTETH_WETH, chain_id=1)

    assert envelope.value.complete is False
    assert len(envelope.value.positions) == 3


def test_existing_public_methods_are_unchanged(morpho_client):
    assert morpho_client.get_market(
        "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"
    ).value.loan_asset.symbol == "USDC"


# --- MorphoDiscovery -------------------------------------------------------------------


def test_discovery_builds_multi_chain_topology(client):
    batch = discovery(client).discover(NOW)

    assert batch.discovered_at == NOW
    assert [c.entity_id for c in batch.chains] == ["chain:1:ethereum", "chain:8453:base"]
    assert {p.entity_id for p in batch.protocols} == {"protocol:1:morpho", "protocol:8453:morpho"}
    assert {v.entity_id for v in batch.vaults} == {
        f"vault:1:{lower(CATALOG_VAULT)}",
        f"vault:1:{lower(NEIGHBOR_VAULT)}",
        f"vault:1:{lower(V2_VAULT)}",
        f"vault:8453:{lower(BASE_VAULT)}",
    }
    assert f"market:8453:{M_BASE}" in {m.entity_id for m in batch.markets}
    assert {o.entity_id for o in batch.oracles} >= {
        f"oracle:1:{lower(ORACLE_WETH_USDC)}",
        f"oracle:1:{lower(ORACLE_WSTETH_WETH)}",
    }
    assert {i.entity_id for i in batch.irms} == {f"irm:1:{lower(IRM)}", f"irm:8453:{lower(IRM)}"}
    assert batch.complete_chain_ids == (1, 8453)
    assert batch.failures == ()
    assert {source.chain_id for source in batch.sources} == {None, 1, 8453}
    ids = [entity.entity_id for entity in batch.entities]
    assert len(ids) == len(set(ids))


def test_discovery_relationships_follow_graph_conventions(client):
    batch = discovery(client).discover(NOW)
    edges = {
        (r.source_entity_id, r.target_entity_id, r.relationship_type): r
        for r in batch.relationships
    }
    vault = f"vault:1:{lower(CATALOG_VAULT)}"
    market = f"market:1:{M_WSTETH_WETH}"

    allocation = edges[(vault, market, RadarRelationshipType.ALLOCATED_TO)]
    assert allocation.weight == Decimal("0.6")
    assert allocation.metadata["supply_assets"] == Decimal("600")
    assert (market, f"asset:1:{lower('0x' + 'e' * 40)}", RadarRelationshipType.LENDS) in edges
    assert (market, f"asset:1:{lower('0x' + 'd' * 40)}", RadarRelationshipType.USES_COLLATERAL) in edges
    assert (market, f"oracle:1:{lower(ORACLE_WSTETH_WETH)}", RadarRelationshipType.USES_ORACLE) in edges
    assert (market, f"irm:1:{lower(IRM)}", RadarRelationshipType.USES_IRM) in edges
    assert (market, "protocol:1:morpho", RadarRelationshipType.DEPLOYED_ON) in edges
    assert (vault, "protocol:1:morpho", RadarRelationshipType.DEPLOYED_ON) in edges
    assert ("protocol:1:morpho", "chain:1:ethereum", RadarRelationshipType.DEPLOYED_ON) in edges
    assert (
        f"vault:1:{lower(V2_VAULT)}",
        f"vault:1:{lower(NEIGHBOR_VAULT)}",
        RadarRelationshipType.ALLOCATED_TO,
    ) in edges
    # A vault with zero assets has no meaningful allocation weight.
    empty = edges[
        (f"vault:1:{lower(NEIGHBOR_VAULT)}", market, RadarRelationshipType.ALLOCATED_TO)
    ]
    assert empty.weight is None
    known = {entity.entity_id for entity in batch.entities}
    assert all(
        r.source_entity_id in known and r.target_entity_id in known
        for r in batch.relationships
    )


def test_idle_markets_do_not_create_null_dependencies(client):
    batch = discovery(client).discover(NOW)
    idle = f"market:1:{M_IDLE_USDC}"

    targets = {
        r.relationship_type
        for r in batch.relationships
        if r.source_entity_id == idle
    }
    assert targets == {RadarRelationshipType.LENDS, RadarRelationshipType.DEPLOYED_ON}
    assert all(
        not entity.address_or_key.startswith("0x0000000000000000000000000000000000000000")
        for entity in batch.entities
    )


def test_catalog_entities_and_their_dependencies_are_visible(client):
    batch = discovery(client).discover(NOW)
    by_id = {entity.entity_id: entity for entity in batch.entities}

    catalog_vault = by_id[f"vault:1:{lower(CATALOG_VAULT)}"]
    assert catalog_vault.catalog is True
    assert catalog_vault.visible is True
    assert catalog_vault.metadata["curator"] == "0x8888888888888888888888888888888888888888"
    assert catalog_vault.metadata["version"] == "v1"
    assert by_id[f"market:1:{M_WSTETH_WETH}"].visible is True
    assert by_id[f"oracle:1:{lower(ORACLE_WSTETH_WETH)}"].visible is True
    assert by_id["chain:1:ethereum"].visible is True
    assert by_id[f"vault:1:{lower(NEIGHBOR_VAULT)}"].visible is False
    assert by_id[f"vault:1:{lower(NEIGHBOR_VAULT)}"].catalog is False
    assert by_id[f"market:8453:{M_BASE}"].visible is False
    assert by_id[f"market:1:{M_WSTETH_WETH}"].metadata["lltv"] == Decimal("0.965")


def test_catalog_markets_are_flagged(client):
    batch = discovery(
        client,
        catalog=(
            catalog_entry(
                entity_id="catalog-market",
                entity_type="morpho_market",
                address_or_market_id=M_WETH_USDC,
                supported_modes=["lender", "borrower"],
                lltv="0.86",
            ),
        ),
    ).discover(NOW)

    market = next(m for m in batch.markets if m.entity_id == f"market:1:{M_WETH_USDC}")
    assert market.catalog is True and market.visible is True


def test_one_failing_chain_does_not_discard_the_others(client, api):
    api.failing_chains = {8453}

    batch = discovery(client).discover(NOW)

    assert batch.complete_chain_ids == (1,)
    assert {f.chain_id for f in batch.failures} == {8453}
    assert all("8453" in f.message or f.chain_id == 8453 for f in batch.failures)
    assert f"vault:1:{lower(CATALOG_VAULT)}" in {v.entity_id for v in batch.vaults}
    assert not [v for v in batch.vaults if v.chain_id == 8453]
    # The chain itself remains part of the topology so its state can be reported.
    assert "chain:8453:base" in {c.entity_id for c in batch.chains}


def test_failed_chain_listing_falls_back_to_configured_and_catalog_chains(client, api):
    api.failing_roots = {"chains"}

    batch = discovery(client, chain_ids=(1,)).discover(NOW)

    assert [f.kind for f in batch.failures] == ["chains"]
    assert [c.entity_id for c in batch.chains] == ["chain:1:ethereum"]
    assert batch.complete_chain_ids == (1,)


def test_chain_allowlist_restricts_discovery(client, api):
    batch = discovery(client, chain_ids=(8453,)).discover(NOW)

    assert [c.chain_id for c in batch.chains] == [8453]
    assert all(
        r.get("variables", {}).get("chainIds", [8453]) == [8453] for r in api.requests
    )


def test_rejected_entities_keep_the_chain_out_of_tombstoning(client, api):
    api.world["markets"][1][0]["state"]["supplyAssets"] = -5

    batch = discovery(client).discover(NOW)

    assert 1 not in batch.complete_chain_ids
    assert 8453 in batch.complete_chain_ids
    assert any(f.entity_id == f"market:1:{M_WETH_USDC}" for f in batch.failures)
    assert f"market:1:{M_WSTETH_WETH}" in {m.entity_id for m in batch.markets}


def test_discovery_records_expose_records_for_collectors(client):
    batch = discovery(client).discover(NOW)

    assert {m.market_id for m in batch.market_records} >= {M_WETH_USDC, M_BASE}
    assert {v.address for v in batch.vault_records} >= {CATALOG_VAULT, BASE_VAULT}
