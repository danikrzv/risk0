import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.ethereum import EthereumClient
from app.adapters.morpho_oracle import (
    ORACLE_V1,
    ORACLE_V2,
    FeedKind,
    MorphoOracleReader,
    ThirdPartyFeedRegistry,
    default_third_party_registry_path,
    is_power_of_ten,
    parse_provider,
)
from app.config import Settings
from tests.fixtures.rpc.fake_chain import FakeChain, Feed


NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
ORACLE = "0x0f948cba8231db7898ef36a4212581ad7b1b4580"
ETH_USD = "0xc0053f3fbccd593758258334dfce24c2a9a673ad"
USDC_USD = "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6"
ADAPTER = "0x905b7dabcd3ce6b792d874e303d336424cdb1421"


def ts(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


@pytest.fixture
def chain() -> FakeChain:
    chain = FakeChain()
    chain.add_feed(ETH_USD, Feed("ETH / USD", 8, {2: [(18283, 250834288397, ts(0.2))]}))
    chain.add_feed(USDC_USD, Feed("USDC / USD", 8, {4: [(28, 99988047, ts(20))]}))
    chain.add_rate_adapter(ADAPTER, "wstETH/stETH exchange rate", 1246013674805236042)
    return chain


def reader(chain: FakeChain) -> MorphoOracleReader:
    client = EthereumClient(Settings("https://rpc.invalid/x"), rpc_call=chain.rpc)
    return MorphoOracleReader(client)


def test_v2_oracle_is_decoded_into_classified_feeds(chain):
    chain.add_oracle(
        ORACLE, base_feed_1=ETH_USD, quote_feed_1=USDC_USD, scale_factor=10**16, price=2508 * 10**24
    )

    oracle = reader(chain).decode(ORACLE)

    assert oracle is not None
    assert oracle.oracle_type == ORACLE_V2
    assert oracle.scale_factor == 10**16
    assert oracle.base_vault is None and oracle.quote_vault is None
    assert [(feed.role, feed.address, feed.kind, feed.description, feed.decimals) for feed in oracle.feeds] == [
        ("BASE_FEED_1", ETH_USD, FeedKind.CHAINLINK, "ETH / USD", 8),
        ("QUOTE_FEED_1", USDC_USD, FeedKind.CHAINLINK, "USDC / USD", 8),
    ]
    assert oracle.decodable
    # Two multicalls: the oracle getters, then every feed's metadata.
    assert chain.eth_calls == 2


def test_v1_oracle_with_a_single_vault_getter_is_decoded(chain):
    chain.add_oracle(ORACLE, base_feed_1=ETH_USD, version="v1")

    oracle = reader(chain).decode(ORACLE)

    assert oracle is not None and oracle.oracle_type == ORACLE_V1


def test_documented_rate_adapter_is_classified_and_cannot_be_stale(chain):
    chain.add_oracle(ORACLE, base_feed_1=ADAPTER)

    oracle = reader(chain).decode(ORACLE)

    assert oracle is not None
    (feed,) = oracle.feeds
    assert feed.kind is FeedKind.EXCHANGE_RATE_ADAPTER
    assert oracle.decodable


def test_undocumented_zero_time_feed_without_a_second_block_read_is_unknown(chain):
    # Without Multicall3's block getters the computed-on-read rule cannot be
    # confirmed at a second block, so the feed stays unknown (never healthy).
    other = "0x" + "ab" * 20
    chain.add_rate_adapter(other, "something/else exchange rate", 10**18)
    chain.add_oracle(ORACLE, base_feed_1=other)

    oracle = reader(chain).decode(ORACLE)

    assert oracle is not None
    assert oracle.feeds[0].kind is FeedKind.UNKNOWN
    assert not oracle.decodable


# Non-Chainlink feeds (the chain reports block times, as mainnet Multicall3 does)

FEED = "0x" + "ab" * 20


def classify(chain: FakeChain, **feeds: str) -> list:
    chain.block_timestamp = ts(0)
    chain.add_oracle(ORACLE, **feeds)
    oracle = reader(chain).decode(ORACLE)
    assert oracle is not None
    return list(oracle.feeds)


def test_zero_update_time_on_two_blocks_is_computed_on_read(chain):
    chain.add_rate_adapter(FEED, "ETH+/USD exchange rate adapter", 2757 * 10**18)

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.COMPUTED_ON_READ
    # The confirmation read is pinned to the previous block.
    assert chain.blocks_called[-1] == hex(chain.block_number - 1)


def test_update_time_equal_to_each_read_block_is_computed_on_read(chain):
    chain.add_computed_feed(FEED, "Ojo PT Feed Pendle Chainlink-compatible Oracle", 981859051090815957)

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.COMPUTED_ON_READ
    assert feed.provider == "Ojo"


def test_a_live_rate_that_reads_exactly_one_is_not_a_constant_scaler(chain):
    # InfiniFi's RT oracle reads 1e18 at 18 decimals but is a live rate.
    chain.add_computed_feed(FEED, "Chainlink-formatted InfiniFi RT Oracle", 10**18)

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.COMPUTED_ON_READ


def test_dummy_power_of_ten_feed_is_a_constant_scaler(chain):
    chain.add_computed_feed(
        FEED, "Dummy feed with 12 decimals", 10**12, decimals=12, round_id=1, any_round=True
    )

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.CONSTANT_SCALER
    assert feed.constant_answer == 10**12


@pytest.mark.parametrize("answer", [10**12 + 1, 5 * 10**11], ids=["not-power", "half"])
def test_a_scaler_answer_must_be_an_exact_power_of_ten(chain, answer):
    chain.add_computed_feed(FEED, "Dummy feed", answer, decimals=12, round_id=1, any_round=True)

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.COMPUTED_ON_READ


def test_aggregator_v3_feed_without_phase_is_third_party_with_provider(chain):
    rounds = [(1, 10**13, ts(24 * 67)), (2, 10**13, ts(24 * 18))]
    chain.add_third_party_feed(FEED, "mGLOeuro/EUR", 8, rounds)

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.THIRD_PARTY
    assert feed.provider == "Midas"
    assert feed.history_readable
    assert feed.decimals == 8


def test_third_party_feed_whose_previous_round_reverts_has_no_readable_history(chain):
    round_id = (2 << 64) | 922  # phase-like id, but no phaseId() and no getRoundData
    chain.add_third_party_feed(
        FEED, "Custom price feed for wstETH / ETH", 8, [(round_id, 124560829, ts(22))], history=False
    )

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.THIRD_PARTY
    assert feed.provider == "Custom"
    assert not feed.history_readable


def test_push_feed_updated_in_the_latest_block_only_is_third_party(chain):
    chain.block_timestamp = ts(0)
    rounds = [(1, 10**8, ts(6)), (2, 10**8, ts(0))]  # round 2 lands in the latest block
    chain.add_third_party_feed(FEED, "RedStone Price Feed for strUSD_FUNDAMENTAL", 8, rounds)

    class AtBlock:
        """Round 2 does not exist yet at the previous block."""

        def __init__(self, chain):
            self.chain = chain

        def rpc(self, method, params):
            if len(params) > 1 and params[1] != "latest":
                rounds[:] = rounds[:1]
                try:
                    return self.chain.rpc(method, params)
                finally:
                    rounds.append((2, 10**8, ts(0)))
            return self.chain.rpc(method, params)

    chain.add_oracle(ORACLE, base_feed_1=FEED)
    client = EthereumClient(Settings("https://rpc.invalid/x"), rpc_call=AtBlock(chain).rpc)
    (feed,) = MorphoOracleReader(client).decode(ORACLE).feeds

    assert feed.kind is FeedKind.THIRD_PARTY
    assert feed.provider == "RedStone"


@pytest.mark.parametrize(
    ("description", "provider"),
    [
        ("RedStone Price Feed for oTFYe_FUNDAMENTAL", "RedStone"),
        ("Ojo PT Feed Pendle Chainlink-compatible Oracle", "Ojo"),
        ("mGLOeuro/EUR", "Midas"),
        ("Chronicle wstETH/USD", "Chronicle"),
        ("Pyth ETH/USD", "Pyth"),
        ("API3 ETH/USD", "API3"),
        ("Custom price feed for wstETH / ETH", "Custom"),
        ("ETH / USD", "unknown"),
        (None, "unknown"),
    ],
)
def test_provider_is_parsed_from_the_description_for_display(description, provider):
    assert parse_provider(description) == provider


def test_feed_without_an_answer_is_unknown(chain):
    chain.add_third_party_feed(FEED, "RedStone Price Feed for X", 8, [(1, 0, ts(1))])

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.UNKNOWN


def test_feed_updated_in_the_future_is_unknown(chain):
    chain.add_third_party_feed(FEED, "RedStone Price Feed for X", 8, [(1, 10**8, ts(-1))])

    (feed,) = classify(chain, base_feed_1=FEED)

    assert feed.kind is FeedKind.UNKNOWN


def test_chainlink_only_oracles_need_no_second_block_read(chain):
    chain.block_timestamp = ts(0)
    chain.add_oracle(ORACLE, base_feed_1=ETH_USD, quote_feed_1=USDC_USD)

    reader(chain).decode(ORACLE)

    assert chain.eth_calls == 2
    assert set(chain.blocks_called) == {"latest"}


def test_power_of_ten_rule():
    assert [is_power_of_ten(value) for value in (1, 10, 10**18, 0, 2, 20, 1001, -10)] == [
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
    ]


# Published heartbeat registry


def write_registry(tmp_path, feeds) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"chain_id": 1, "feeds": feeds}))
    return path


def entry(**overrides):
    item = {
        "address": "0x62C69EE85613942D53cC16Fc480E0606c7611b5d",
        "description": "RedStone Price Feed for oTFYe_FUNDAMENTAL",
        "provider": "RedStone",
        "heartbeat_seconds": 86400,
        "source_url": "https://example.org/manifest.json",
    }
    item.update(overrides)
    return item


def test_registry_entry_applies_only_with_the_registered_description(tmp_path):
    registry = ThirdPartyFeedRegistry.load(write_registry(tmp_path, [entry()]))

    found = registry.lookup("0x62c69ee85613942d53cc16fc480e0606c7611b5d", "RedStone Price Feed for oTFYe_FUNDAMENTAL")

    assert found is not None and found.heartbeat_seconds == 86400 and found.provider == "RedStone"
    assert registry.lookup("0x62c69ee85613942d53cc16fc480e0606c7611b5d", "something else") is None
    assert registry.lookup("0x" + "11" * 20, "RedStone Price Feed for oTFYe_FUNDAMENTAL") is None


@pytest.mark.parametrize(
    "bad",
    [
        {"heartbeat_seconds": 0},
        {"heartbeat_seconds": "86400"},
        {"heartbeat_seconds": True},
        {"source_url": "http://example.org"},
        {"address": "0x1234"},
    ],
)
def test_registry_rejects_entries_without_a_valid_published_value(tmp_path, bad):
    with pytest.raises(ValueError):
        ThirdPartyFeedRegistry.load(write_registry(tmp_path, [entry(**bad)]))


def test_registry_rejects_duplicates(tmp_path):
    with pytest.raises(ValueError):
        ThirdPartyFeedRegistry.load(write_registry(tmp_path, [entry(), entry()]))


def test_version_controlled_registry_has_sourced_entries_only():
    path = default_third_party_registry_path()
    registry = ThirdPartyFeedRegistry.load(path)
    payload = json.loads(path.read_text())

    assert len(registry) == len(payload["feeds"]) >= 1
    for item in payload["feeds"]:
        assert item["source_url"].startswith("https://")
        assert item["source_quote"]
    # No published cadence exists for the Midas mGLOeuro/EUR NAV feed.
    assert registry.lookup("0x8b7b1689396ef4468df1c0fab181d29b47089609", "mGLOeuro/EUR") is None
    assert registry.lookup(
        "0x7fda577c085330e69df3179a2a5867751a9d0caa", "RedStone Price Feed for strUSD_FUNDAMENTAL"
    ).heartbeat_seconds == 21600


def test_a_contract_that_is_not_a_morpho_oracle_is_not_decoded(chain):
    assert reader(chain).decode(ETH_USD) is None  # a feed has no BASE_FEED_1
    assert reader(chain).decode("0x" + "cd" * 20) is None  # no code at all


def test_state_reads_price_and_every_feed_in_one_multicall(chain):
    chain.add_oracle(ORACLE, base_feed_1=ETH_USD, quote_feed_1=USDC_USD, price=123)
    oracle_reader = reader(chain)
    oracle = oracle_reader.decode(ORACLE)
    chain.eth_calls = 0

    state = oracle_reader.read_state(oracle)

    assert chain.eth_calls == 1
    assert state.price == 123
    assert [item.updated_at for item in state.latest] == [ts(0.2), ts(20)]
    assert state.latest[0].answer == 250834288397
