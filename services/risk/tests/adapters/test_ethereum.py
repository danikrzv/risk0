import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.ethereum import (
    EthereumClient,
    EthereumProviderError,
    UnsupportedFeedError,
)
from app.config import Settings


FIXTURES = Path(__file__).parents[1] / "fixtures" / "rpc"
START = datetime(2026, 10, 9, 9, tzinfo=UTC)
END = datetime(2026, 10, 9, 12, tzinfo=UTC)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://rpc.invalid/v2/placeholder")
    return Settings.from_environment()


def test_chainlink_logs_honor_decimals_sort_and_resolve_duplicates(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    log_filters = []

    def rpc_call(method: str, params: list[object]) -> object:
        if method == "eth_call":
            call = params[0]
            if call["data"] == "0x313ce567":
                return fixture["decimals"]
            if call["data"] == "0x245a7bfc":
                return fixture["aggregator"]
        if method == "eth_getLogs":
            log_filters.append(params[0])
            return fixture["logs"]
        raise AssertionError(f"unexpected method: {method}")

    client = EthereumClient(settings(monkeypatch), rpc_call=rpc_call)

    observations = client.get_chainlink_observations("ETH/USD", START, END)

    assert [item.block_number for item in observations] == [100, 101]
    assert [item.log_index for item in observations] == [1, 0]
    assert [item.value for item in observations] == [
        Decimal("1.01"),
        Decimal("1.02"),
    ]
    assert [item.timestamp for item in observations] == [
        datetime(2026, 10, 9, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 9, 11, 2, tzinfo=UTC),
    ]
    assert log_filters[0]["address"] == "0x1234567890abcdef1234567890abcdef12345678"


def test_block_number_and_gas_price_are_parsed_without_binary_float(monkeypatch):
    results = {"eth_blockNumber": "0x12d687", "eth_gasPrice": "0x6fc23ac00"}
    client = EthereumClient(
        settings(monkeypatch), rpc_call=lambda method, params: results[method]
    )

    assert client.get_block_number() == 1_234_567
    assert client.get_gas_price() == Decimal("30000000000")


def test_unsupported_feed_is_explicit_and_never_calls_provider(monkeypatch):
    client = EthereumClient(
        settings(monkeypatch),
        rpc_call=lambda method, params: pytest.fail("provider must not be called"),
    )

    with pytest.raises(UnsupportedFeedError, match="unsupported Chainlink feed"):
        client.get_chainlink_observations("NOT/A/FEED", START, END)


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


def test_settings_requires_rpc_url_from_environment(monkeypatch):
    monkeypatch.delenv("ETHEREUM_RPC_URL", raising=False)

    with pytest.raises(ValueError, match="ETHEREUM_RPC_URL"):
        Settings.from_environment()
