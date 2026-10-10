from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.conftest import MORPHO_BASE_URL


WSTETH_WETH = "0xb8fc70e82bc5bb53e773626fcc6a23f7eefa036918d7ef216ecfb1950a94a85e"
WETH_USDC = "0x94b823e6bd8ea533b4e33fbc307faea0b307301bc48763acc4d4aa4def7636cd"
MEV_CAPITAL_WETH = "0x9a8bC3B04b7f3D87cfC09ba407dCED575f2d61D8"
BELEM_RWA_EURCV = "0xD6d4b804014EF27836dBe9f8f6Bf6c71251548Ec"
INDEXED_BLOCK = 26161049


def test_market_converts_integer_units_with_token_decimals(morpho_client):
    market = morpho_client.get_market(WETH_USDC).value

    assert market.loan_asset.symbol == "USDC"
    assert market.loan_asset.decimals == 6
    assert market.collateral_asset.symbol == "WETH"
    assert market.collateral_asset.decimals == 18
    assert market.total_supply_assets == Decimal("9408096.071094")
    assert market.total_borrow_assets == Decimal("8362152.970156")
    assert market.liquidity_assets == Decimal("1045943.100938")
    assert market.utilization == Decimal("8362152970156") / Decimal("9408096071094")


def test_market_preserves_exact_lltv_and_large_18_decimal_values(morpho_client):
    market = morpho_client.get_market(WSTETH_WETH).value

    assert isinstance(market.lltv, Decimal)
    assert market.lltv == Decimal("0.965")
    assert market.total_supply_assets == Decimal("33740.063099650389784169")
    assert market.total_borrow_assets == Decimal("28328.629178065263301630")


def test_market_usd_metrics_are_decimals_not_binary_floats(morpho_client):
    market = morpho_client.get_market(WETH_USDC).value

    assert market.total_supply_usd == Decimal("9405598.688924348")
    assert market.liquidity_usd == Decimal("1045681.2413280633")
    assert market.loan_asset.price_usd == Decimal("0.9997496425860051")
    assert market.listed is True


def test_market_envelope_carries_source_fetch_time_block_and_freshness(
    morpho_client, clock
):
    envelope = morpho_client.get_market(WETH_USDC)

    assert envelope.source == (
        f"{MORPHO_BASE_URL}/v0/blue/markets/1:{WETH_USDC}/state"
    )
    assert envelope.fetched_at == clock.now
    assert envelope.block_number == INDEXED_BLOCK
    assert envelope.freshness == "fresh"
    assert f"{MORPHO_BASE_URL}/graphql" in envelope.value.sources


def test_vault_v1_snapshot_uses_asset_decimals_and_rest_block(morpho_client, clock):
    envelope = morpho_client.get_vault(MEV_CAPITAL_WETH, "v1")
    vault = envelope.value

    assert vault.version == "v1"
    assert vault.name == "MEV Capital wETH"
    assert vault.asset.symbol == "WETH"
    assert vault.total_assets == Decimal("1857.124314225046976544")
    assert vault.liquidity_assets == Decimal("1857.124314225046976544")
    assert vault.total_assets_usd == Decimal("4628941.175712276")
    assert vault.listed is True
    assert envelope.block_number == INDEXED_BLOCK
    assert envelope.fetched_at == clock.now
    assert envelope.source == f"{MORPHO_BASE_URL}/v0/vaults-v1/1:{MEV_CAPITAL_WETH}/state"


def test_vault_v2_snapshot_reports_liquidity_and_idle_assets(morpho_client):
    envelope = morpho_client.get_vault(BELEM_RWA_EURCV, "v2")
    vault = envelope.value

    assert vault.version == "v2"
    assert vault.asset.symbol == "EURCV"
    assert vault.total_assets == Decimal("4710489.785220272556475282")
    assert vault.liquidity_assets == Decimal("361743.300394771869707524")
    assert vault.idle_assets == Decimal("0.003451938900573403")
    assert vault.liquidity_usd == Decimal("404749.93962627044")
    assert envelope.source == f"{MORPHO_BASE_URL}/v1/vaults-v2/1:{BELEM_RWA_EURCV}/state"
    assert envelope.block_number == INDEXED_BLOCK


@pytest.mark.parametrize(
    ("address", "version", "expected_markets"),
    [
        (MEV_CAPITAL_WETH, "v1", 6),
        (BELEM_RWA_EURCV, "v2", 8),
    ],
)
def test_allocation_weights_sum_to_one_within_one_base_unit(
    morpho_client, address, version, expected_markets
):
    envelope = morpho_client.get_allocations(address, version)
    allocations = envelope.value

    assert len(allocations) == expected_markets
    assert all(isinstance(item.weight, Decimal) for item in allocations)
    assert all(Decimal(0) <= item.weight <= Decimal(1) for item in allocations)
    assert abs(sum(item.weight for item in allocations) - Decimal(1)) <= Decimal(
        "1e-18"
    )
    assert envelope.block_number == INDEXED_BLOCK
    assert envelope.source.endswith(f"/1:{address}/allocations")


def test_v1_allocations_convert_supply_and_expose_dominant_market(morpho_client):
    allocations = morpho_client.get_allocations(MEV_CAPITAL_WETH, "v1").value
    by_market = {item.market_id: item for item in allocations}

    wsteth = by_market[WSTETH_WETH]
    assert wsteth.supply_assets == Decimal("1853.796077972177618759")
    assert wsteth.supply_cap == Decimal("20000")
    assert wsteth.weight > Decimal("0.99")
    assert wsteth.pending_removal is False
    assert by_market[
        "0x2cbfb38723a8d9a2ad1607015591a78cfe3a5949561b39bde42c242b22874ec0"
    ].weight == Decimal(0)


def test_v2_allocations_use_market_caps_and_expected_supply(morpho_client):
    allocations = morpho_client.get_allocations(BELEM_RWA_EURCV, "v2").value
    by_market = {item.market_id: item for item in allocations}

    largest = by_market[
        "0x987b2c1979c6cfd6b7833d9b30c09b96ec6de1666579582d8f17730e20dd19b0"
    ]
    assert largest.supply_assets == Decimal("4040539.765404757307752844")
    assert largest.supply_cap == Decimal("10000000")
    assert largest.adapter_address == "0xA671880d813D4cacacA63984bc0Ab676c400A874"


def test_fresh_cache_hit_avoids_network_within_ttl(morpho_client, morpho_api, clock):
    first = morpho_client.get_market(WETH_USDC)
    requests_after_first = len(morpho_api.requests)
    clock.advance(timedelta(seconds=59))

    second = morpho_client.get_market(WETH_USDC)

    assert len(morpho_api.requests) == requests_after_first
    assert second.freshness == "fresh"
    assert second.value == first.value
    assert second.fetched_at == first.fetched_at


def test_provider_failure_returns_stale_cache_with_original_metadata(
    morpho_client, morpho_api, clock
):
    first = morpho_client.get_market(WETH_USDC)
    clock.advance(timedelta(minutes=5))
    morpho_api.mode = "down"

    stale = morpho_client.get_market(WETH_USDC)

    assert stale.freshness == "stale_usable"
    assert stale.fetched_at == first.fetched_at
    assert stale.block_number == first.block_number
    assert stale.source == first.source
    assert stale.value == first.value


def test_provider_failure_without_cache_raises_unavailable(morpho_client, morpho_api):
    from app.adapters.morpho import MorphoUnavailableError

    morpho_api.mode = "down"

    with pytest.raises(MorphoUnavailableError):
        morpho_client.get_market(WETH_USDC)


def test_server_errors_are_retried_a_bounded_number_of_times(
    morpho_api, cache, clock
):
    from app.adapters.morpho import MorphoClient, MorphoUnavailableError

    sleeps = []
    client = MorphoClient(
        MORPHO_BASE_URL,
        http_client=morpho_api.http_client(),
        cache=cache,
        clock=clock,
        max_attempts=3,
        sleep=sleeps.append,
    )
    morpho_api.mode = "error"

    with pytest.raises(MorphoUnavailableError):
        client.get_market(WETH_USDC)

    assert len(morpho_api.requests) == 3
    assert len(sleeps) == 2


def test_client_applies_request_timeout(morpho_api, cache, clock):
    from app.adapters.morpho import MorphoClient

    client = MorphoClient(
        MORPHO_BASE_URL,
        http_client=morpho_api.http_client(),
        cache=cache,
        clock=clock,
        timeout_seconds=7,
    )
    client.get_market(WETH_USDC)

    assert all(
        request.extensions["timeout"]["read"] == 7 for request in morpho_api.requests
    )


def test_unknown_market_raises_not_found_without_cache_fallback(
    morpho_client, morpho_api, clock
):
    from app.adapters.morpho import MorphoNotFoundError

    morpho_client.get_market(WETH_USDC)
    clock.advance(timedelta(minutes=5))
    morpho_api.remove_market(WETH_USDC)

    with pytest.raises(MorphoNotFoundError):
        morpho_client.get_market(WETH_USDC)


def test_conflicting_lltv_between_rest_and_graphql_is_rejected(
    morpho_client, morpho_api
):
    from app.adapters.morpho import MorphoUnavailableError

    morpho_api.market_graphql(WETH_USDC)["lltv"] = "915000000000000000"

    with pytest.raises(MorphoUnavailableError, match="lltv"):
        morpho_client.get_market(WETH_USDC)


def test_source_urls_never_include_credentials(morpho_api, cache, clock):
    from app.adapters.morpho import MorphoClient

    client = MorphoClient(
        "https://user:secret-token@api.morpho.org/?key=secret-token",
        http_client=morpho_api.http_client(),
        cache=cache,
        clock=clock,
    )
    envelope = client.get_market(WETH_USDC)

    assert "secret-token" not in envelope.source
    assert all("secret-token" not in source for source in envelope.value.sources)


def test_snapshots_reject_binary_floats():
    from app.domain.snapshots import AssetInfo

    with pytest.raises(ValidationError):
        AssetInfo(
            address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            symbol="USDC",
            decimals=6,
            price_usd=0.9997,
        )
