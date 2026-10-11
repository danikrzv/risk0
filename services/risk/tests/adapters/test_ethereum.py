import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.ethereum import (
    MULTICALL_BATCH_SIZE,
    EthereumClient,
    EthereumProviderError,
    UnsupportedFeedError,
)
from app.adapters.multicall import Call
from app.config import Settings
from app.infrastructure.cache import SQLiteCache
from tests.fixtures.rpc.fake_chain import FakeChain, Feed


NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
USDC_USD = "0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6"
BTC_USD = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
WBTC_BTC = "0xfdFD9C85aD200c506Cf9e21F1FD8dd01932FBB23"
ADAPTER = "0x905b7dabcd3ce6b792d874e303d336424cdb1421"
CATALOG = Path(__file__).resolve().parents[4] / "data" / "catalog" / "feeds.ethereum.json"


def ts(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://rpc.invalid/v2/placeholder")
    return Settings.from_environment()


def hourly_feed(phases: dict[int, int], *, first_hours_ago: float, answer: int = 2_000_00000000) -> Feed:
    """Rounds one hour apart, `phases[p]` rounds in phase p, the newest `first_hours_ago - n` ago."""
    total = sum(phases.values())
    feed = Feed("ETH / USD", 8)
    offset = 0
    for phase in sorted(phases):
        feed.phases[phase] = [
            (k, answer + offset + k, ts(first_hours_ago - offset - k + 1))
            for k in range(1, phases[phase] + 1)
        ]
        offset += phases[phase]
    assert offset == total
    return feed


def client_for(chain: FakeChain, monkeypatch, **kwargs) -> EthereumClient:
    return EthereumClient(settings(monkeypatch), rpc_call=chain.rpc, clock=lambda: NOW, **kwargs)


def get_round_reads(chain: FakeChain) -> int:
    return sum(1 for _, selector in chain.calls if selector == "0x9a6fc8f5")


def test_latest_observation_comes_from_latest_round_data_with_decimals(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {7: [(34503, 250698054686, ts(0.5))]}))

    latest = client_for(chain, monkeypatch).get_latest_observation("ETH/USD")

    assert latest is not None
    assert latest.value == Decimal("2506.98054686")
    assert latest.round_id == (7 << 64) | 34503
    assert latest.timestamp == datetime.fromtimestamp(ts(0.5), tz=UTC)
    assert chain.eth_calls == 2  # latestRoundData and decimals, no log scan


def test_signed_negative_answers_are_decimal(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {1: [(1, -150000000, ts(1))]}))

    latest = client_for(chain, monkeypatch).get_latest_observation("ETH/USD")

    assert latest is not None and latest.value == Decimal("-1.50000000")


def test_rounds_walk_back_across_phases_in_bounded_batches(monkeypatch):
    chain = FakeChain()
    feed = chain.add_feed(ETH_USD, hourly_feed({1: 5, 2: 4}, first_hours_ago=9))
    client = client_for(chain, monkeypatch, batch_size=2)

    observations = client.get_chainlink_observations(
        "ETH/USD", NOW - timedelta(hours=8, minutes=30), NOW
    )

    # All rounds from 8.5 h ago: phase 1 rounds 2..5 then phase 2 rounds 1..4, in time order.
    assert [(item.round_id >> 64, item.round_id & 0xFFFF) for item in observations] == [
        (1, 2), (1, 3), (1, 4), (1, 5), (2, 1), (2, 2), (2, 3), (2, 4)
    ]
    assert [item.value for item in observations] == [
        Decimal(feed.phases[p][k - 1][1]).scaleb(-8) for p, k in [(1, 2), (1, 3), (1, 4), (1, 5), (2, 1), (2, 2), (2, 3), (2, 4)]
    ]
    multicalls = [call for call in chain.calls if call[1] == "0x9a6fc8f5"]
    assert len(multicalls) == 9  # (2,4..1), (1,5..2) and (1,1): the batch reaching before the start
    # The previous phase's last round is read from its aggregator directly, never via Multicall3.
    assert (feed.aggregators[1].lower(), "0x668a0f02") in chain.calls


def test_feed_updates_include_the_answer_standing_at_the_start(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, hourly_feed({1: 10}, first_hours_ago=10))
    client = client_for(chain, monkeypatch)
    start = NOW - timedelta(hours=4, minutes=30)

    updates = client.get_feed_updates(ETH_USD, start, NOW)
    in_period = client.get_chainlink_observations("ETH/USD", start, NOW)

    assert updates[0].timestamp < start <= updates[1].timestamp
    assert updates[1:] == in_period
    assert all(start <= item.timestamp for item in in_period)


def test_repeat_reads_only_fetch_new_rounds(monkeypatch):
    chain = FakeChain()
    feed = chain.add_feed(ETH_USD, hourly_feed({3: 50}, first_hours_ago=50))
    client = client_for(chain, monkeypatch, batch_size=20)
    start = NOW - timedelta(hours=60)
    client.get_chainlink_observations("ETH/USD", start, NOW)
    chain.calls.clear()
    chain.eth_calls = 0

    client.get_chainlink_observations("ETH/USD", start, NOW)

    assert chain.eth_calls == 1  # latestRoundData only
    assert get_round_reads(chain) == 0

    feed.phases[3].extend((k, 1, ts(-1)) for k in range(51, 54))
    chain.calls.clear()
    observations = client.get_chainlink_observations("ETH/USD", start, NOW + timedelta(hours=2))

    assert get_round_reads(chain) == 3
    assert len(observations) == 53


def test_new_rounds_in_a_new_phase_are_fetched_across_the_phase_change(monkeypatch):
    chain = FakeChain()
    feed = chain.add_feed(ETH_USD, hourly_feed({1: 10}, first_hours_ago=12))
    client = client_for(chain, monkeypatch)
    client.get_chainlink_observations("ETH/USD", NOW - timedelta(hours=20), NOW)
    feed.phases[2] = [(1, 7, ts(1)), (2, 8, ts(0.5))]
    chain.add_feed(ETH_USD, feed)
    chain.calls.clear()

    observations = client.get_chainlink_observations("ETH/USD", NOW - timedelta(hours=20), NOW)

    assert get_round_reads(chain) == 2
    assert [item.round_id >> 64 for item in observations[-3:]] == [1, 2, 2]


def test_an_earlier_start_extends_the_cached_history_backwards(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, hourly_feed({1: 40}, first_hours_ago=40))
    client = client_for(chain, monkeypatch, batch_size=5)
    client.get_chainlink_observations("ETH/USD", NOW - timedelta(hours=10), NOW)
    first = get_round_reads(chain)
    chain.calls.clear()

    observations = client.get_chainlink_observations("ETH/USD", NOW - timedelta(hours=30), NOW)

    assert first == 15  # three batches of five reach a round before the 10-hour start
    assert get_round_reads(chain) == 20  # four more batches reach before the 30-hour start
    assert len(observations) == 30


def test_rounds_persist_in_the_sqlite_cache_across_clients(monkeypatch, tmp_path):
    chain = FakeChain()
    chain.add_feed(ETH_USD, hourly_feed({2: 30}, first_hours_ago=30))
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    first = client_for(chain, monkeypatch, cache=cache).get_chainlink_observations(
        "ETH/USD", NOW - timedelta(hours=24), NOW
    )
    chain.calls.clear()
    chain.eth_calls = 0

    second = client_for(chain, monkeypatch, cache=cache).get_chainlink_observations(
        "ETH/USD", NOW - timedelta(hours=24), NOW
    )

    assert second == first
    assert chain.eth_calls == 1  # latestRoundData; decimals and rounds come from the cache


def test_missing_rounds_are_skipped(monkeypatch):
    chain = FakeChain()
    feed = hourly_feed({1: 6}, first_hours_ago=6)
    del feed.phases[1][2]  # round 3 is absent: getRoundData returns zeros
    chain.add_feed(ETH_USD, feed)

    observations = client_for(chain, monkeypatch).get_chainlink_observations(
        "ETH/USD", NOW - timedelta(hours=10), NOW
    )

    assert [item.round_id & 0xFF for item in observations] == [1, 2, 4, 5, 6]


def test_a_rate_adapter_has_no_round_history(monkeypatch):
    chain = FakeChain()
    chain.add_rate_adapter(ADAPTER, "wstETH/stETH exchange rate", 1246013674805236042)

    with pytest.raises(UnsupportedFeedError):
        client_for(chain, monkeypatch).get_feed_updates(ADAPTER, NOW - timedelta(days=1), NOW)


def test_unsupported_feed_is_explicit_and_never_calls_provider(monkeypatch):
    client = EthereumClient(
        settings(monkeypatch),
        rpc_call=lambda method, params: pytest.fail("provider must not be called"),
    )

    with pytest.raises(UnsupportedFeedError, match="unsupported Chainlink feed"):
        client.get_chainlink_observations("NOT/A/FEED", NOW - timedelta(hours=1), NOW)


def test_composite_feed_is_the_product_at_every_component_update(monkeypatch):
    chain = FakeChain()
    chain.add_feed(WBTC_BTC, Feed("WBTC / BTC", 8, {1: [(1, 99900000, ts(30)), (2, 99800000, ts(2))]}))
    chain.add_feed(
        BTC_USD,
        Feed("BTC / USD", 8, {1: [(1, 8000000000000, ts(3)), (2, 8100000000000, ts(1))]}),
    )
    client = client_for(chain, monkeypatch)

    observations = client.get_chainlink_observations("WBTC/USD", NOW - timedelta(hours=4), NOW)
    latest = client.get_latest_observation("WBTC/USD")

    assert [item.value for item in observations] == [
        Decimal("0.999") * Decimal("80000"),
        Decimal("0.998") * Decimal("80000"),
        Decimal("0.998") * Decimal("81000"),
    ]
    assert latest is not None
    assert latest.value == Decimal("0.998") * Decimal("81000")
    assert latest.timestamp == datetime.fromtimestamp(ts(2), tz=UTC)  # the older component


def test_aggregate_splits_calls_into_bounded_multicalls(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {1: [(1, 1, ts(1))]}))
    client = client_for(chain, monkeypatch, batch_size=3)

    results = client.aggregate([Call(ETH_USD, "0x313ce567")] * 7 + [Call(ETH_USD, "0x12345678")])

    assert chain.eth_calls == 3
    assert [item.success for item in results] == [True] * 7 + [False]
    assert MULTICALL_BATCH_SIZE == 1000


def test_provider_failure_during_a_walk_is_sanitized(monkeypatch):
    chain = FakeChain()
    chain.add_feed(ETH_USD, hourly_feed({1: 5}, first_hours_ago=5))
    chain.fail = True

    with pytest.raises(EthereumProviderError) as error:
        client_for(chain, monkeypatch).get_chainlink_observations(
            "ETH/USD", NOW - timedelta(hours=8), NOW
        )

    assert "rpc.example.invalid" not in str(error.value)
    assert "fake-rpc-credential" not in str(error.value)
    assert error.value.__cause__ is None


def test_feed_catalog_entries_are_unique_and_composites_resolve(monkeypatch):
    payload = json.loads(CATALOG.read_text())
    client = EthereumClient(settings(monkeypatch), rpc_call=lambda *_: pytest.fail("no calls"))

    addresses = [item["address"].lower() for item in payload["feeds"] if "address" in item]
    assert len(addresses) == len(set(addresses))
    assert all(item.get("description") for item in payload["feeds"])
    assert client.feed("WBTC/USD").components == ("WBTC/BTC", "BTC/USD")
    assert client.feed_by_address(ETH_USD.lower()).feed_id == "ETH/USD"
    assert "exchange rate" in client.feed_notes()["stETH/USD"]


def test_block_number_and_gas_price_are_parsed_without_binary_float(monkeypatch):
    results = {"eth_blockNumber": "0x12d687", "eth_gasPrice": "0x6fc23ac00"}
    client = EthereumClient(
        settings(monkeypatch), rpc_call=lambda method, params: results[method]
    )

    assert client.get_block_number() == 1_234_567
    assert client.get_gas_price() == Decimal("30000000000")


def test_chain_id_is_parsed(monkeypatch):
    client = EthereumClient(
        settings(monkeypatch), rpc_call=lambda method, params: {"eth_chainId": "0x1"}[method]
    )

    assert client.get_chain_id() == 1


def test_provider_failures_do_not_expose_rpc_url_or_token(monkeypatch):
    secret = "do-not-leak-token"
    monkeypatch.setenv("ETHEREUM_RPC_URL", f"https://rpc.invalid/v2/{secret}")

    def failing_rpc(method: str, params: list[object]) -> object:
        raise RuntimeError(f"provider failed at https://rpc.invalid/v2/{secret}")

    client = EthereumClient(Settings.from_environment(), rpc_call=failing_rpc)

    with pytest.raises(EthereumProviderError) as error:
        client.get_block_number()

    assert secret not in str(error.value)
    assert "https://" not in str(error.value)
    assert error.value.__cause__ is None


def test_existing_provider_errors_are_resanitized(monkeypatch):
    secret = "existing-provider-secret"
    client = EthereumClient(
        settings(monkeypatch),
        rpc_call=lambda method, params: (_ for _ in ()).throw(
            EthereumProviderError(f"https://rpc.invalid/{secret}")
        ),
    )

    with pytest.raises(EthereumProviderError) as error:
        client.get_block_number()

    assert secret not in str(error.value)
    assert "https://" not in str(error.value)
    assert error.value.__cause__ is None


def test_malformed_json_rpc_payload_is_sanitized(monkeypatch):
    secret = "malformed-payload-token"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "error": {"message": secret}}

    monkeypatch.setattr("app.adapters.ethereum.httpx.post", lambda *args, **kwargs: Response())
    client = EthereumClient(settings(monkeypatch))

    with pytest.raises(EthereumProviderError) as error:
        client.get_block_number()

    assert secret not in str(error.value)
    assert error.value.__cause__ is None


def test_settings_requires_rpc_url_from_environment(monkeypatch):
    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)

    with pytest.raises(ValueError, match="ETHEREUM_RPC_URL"):
        Settings.from_environment()


def test_settings_repr_redacts_rpc_url(monkeypatch):
    secret = "repr-secret-token"
    monkeypatch.setenv("ETHEREUM_RPC_URL", f"https://rpc.invalid/{secret}")

    rendered = repr(Settings.from_environment())

    assert secret not in rendered
    assert "https://" not in rendered
    assert "<redacted>" in rendered
