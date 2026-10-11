from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.ethereum import EthereumClient
from app.adapters.morpho_oracle import (
    ORACLE_V1,
    ORACLE_V2,
    FeedKind,
    MorphoOracleReader,
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


def test_undocumented_feed_without_rounds_is_unknown(chain):
    other = "0x" + "ab" * 20
    chain.add_rate_adapter(other, "something/else exchange rate", 10**18)
    chain.add_oracle(ORACLE, base_feed_1=other)

    oracle = reader(chain).decode(ORACLE)

    assert oracle is not None
    assert oracle.feeds[0].kind is FeedKind.UNKNOWN
    assert not oracle.decodable


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
