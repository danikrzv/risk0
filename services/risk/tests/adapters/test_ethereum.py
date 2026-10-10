import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from copy import deepcopy

import pytest

from app.adapters.ethereum import (
    EthereumClient,
    EthereumProviderError,
    UnsupportedFeedError,
)
from app.config import Settings
from app.history.normalization import normalize_hourly


FIXTURES = Path(__file__).parents[1] / "fixtures" / "rpc"
START = datetime(2026, 10, 9, 9, tzinfo=UTC)
END = datetime(2026, 10, 9, 12, tzinfo=UTC)
DEPLOYMENT_BLOCK = 10_606_501


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ETHEREUM_RPC_URL", "https://rpc.invalid/v2/placeholder")
    return Settings.from_environment()


def rpc_for_logs(fixture: dict, logs: list[dict], log_filters=None):
    def rpc_call(method: str, params: list[object]) -> object:
        if method == "eth_blockNumber":
            return hex(DEPLOYMENT_BLOCK + 200)
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            timestamp = int(START.timestamp()) - 3_600 + (block - DEPLOYMENT_BLOCK) * 60
            return {"number": hex(block), "timestamp": hex(timestamp)}
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":
                return fixture["decimals"]
            if data == "0x58303b10":
                return hex(1)
            if data.startswith("0xc1597304"):
                return fixture["aggregator"]
            raise AssertionError(data)
        if method == "eth_getLogs":
            if log_filters is not None:
                log_filters.append(params[0])
            return logs
        raise AssertionError(f"unexpected method: {method}")

    return rpc_call


def test_chainlink_logs_honor_decimals_sort_and_resolve_duplicates(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    log_filters = []

    client = EthereumClient(
        settings(monkeypatch),
        rpc_call=rpc_for_logs(fixture, fixture["logs"], log_filters),
    )

    observations = client.get_chainlink_observations("ETH/USD", START, END)

    assert [item.block_number for item in observations] == [100, 100, 101, 101]
    assert [item.log_index for item in observations] == [0, 1, 0, 1]
    assert [item.value for item in observations] == [
        Decimal("1"),
        Decimal("1.01"),
        Decimal("1.02"),
        Decimal("1.03"),
    ]
    assert [item.timestamp for item in observations] == [
        datetime(2026, 10, 9, 10, 0, tzinfo=UTC),
        datetime(2026, 10, 9, 10, 5, tzinfo=UTC),
        datetime(2026, 10, 9, 11, 2, tzinfo=UTC),
        datetime(2026, 10, 9, 11, 3, tzinfo=UTC),
    ]
    assert log_filters[0]["address"] == "0x1234567890abcdef1234567890abcdef12345678"

    hourly = normalize_hourly(observations, maximum_gap=timedelta(hours=6))
    assert [item.value for item in hourly.observations] == [
        Decimal("1.01"),
        Decimal("1.03"),
    ]
    assert hourly.source == "chainlink:ETH/USD"


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


def test_malformed_topics_are_sanitized(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    malformed = deepcopy(fixture["logs"][0])
    malformed["topics"][1] = "0xnot-hex-provider-token"

    client = EthereumClient(
        settings(monkeypatch), rpc_call=rpc_for_logs(fixture, [malformed])
    )

    with pytest.raises(EthereumProviderError) as error:
        client.get_chainlink_observations("ETH/USD", START, END)

    assert "provider-token" not in str(error.value)
    assert error.value.__cause__ is None


def test_malformed_log_hex_fields_are_sanitized(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    malformed = deepcopy(fixture["logs"][0])
    malformed["transactionHash"] = "0xprivate-provider-token"

    client = EthereumClient(
        settings(monkeypatch), rpc_call=rpc_for_logs(fixture, [malformed])
    )

    with pytest.raises(EthereumProviderError) as error:
        client.get_chainlink_observations("ETH/USD", START, END)

    assert "provider-token" not in str(error.value)
    assert error.value.__cause__ is None


def test_signed_negative_chainlink_answers_are_decimal(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    negative = deepcopy(fixture["logs"][0])
    negative["topics"][1] = f"0x{(1 << 256) - 100_000_000:064x}"

    observations = EthereumClient(
        settings(monkeypatch), rpc_call=rpc_for_logs(fixture, [negative])
    ).get_chainlink_observations("ETH/USD", START, END)

    assert observations[0].value == Decimal("-1")


def test_phase_history_uses_bounded_period_block_chunks(monkeypatch):
    fixture = load_fixture("chainlink-logs.json")
    genesis = 1_790_000_000
    phase_addresses = {
        1: "0x1111111111111111111111111111111111111111",
        2: "0x2222222222222222222222222222222222222222",
    }
    log_filters = []

    def phase_log(block: int, phase: int) -> dict:
        timestamp = genesis + block * 12
        answer = phase * 100_000_000
        return {
            "address": phase_addresses[phase],
            "blockNumber": hex(block),
            "logIndex": "0x0",
            "transactionHash": f"0x{phase:064x}",
            "topics": [
                "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f",
                f"0x{answer:064x}",
                f"0x{phase:064x}",
            ],
            "data": f"0x{timestamp:064x}",
        }

    def word_address(address: str) -> str:
        return f"0x{'0' * 24}{address[2:]}"

    def rpc_call(method: str, params: list[object]) -> object:
        if method == "eth_blockNumber":
            return hex(300)
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return {"number": hex(block), "timestamp": hex(genesis + block * 12)}
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":
                return fixture["decimals"]
            if data == "0x58303b10":
                return hex(2)
            if data.startswith("0xc1597304"):
                phase = int(data[-64:], 16)
                return word_address(phase_addresses[phase])
            raise AssertionError(data)
        if method == "eth_getLogs":
            log_filter = params[0]
            log_filters.append(log_filter)
            start_block = int(log_filter["fromBlock"], 16)
            end_block = int(log_filter["toBlock"], 16)
            phase = 1 if log_filter["address"] == phase_addresses[1] else 2
            event_block = 150 if phase == 1 else 200
            return (
                [phase_log(event_block, phase)]
                if start_block <= event_block <= end_block
                else []
            )
        raise AssertionError(method)

    start = datetime.fromtimestamp(genesis + 120 * 12, tz=UTC)
    end = datetime.fromtimestamp(genesis + 220 * 12, tz=UTC)
    client = EthereumClient(
        settings(monkeypatch), rpc_call=rpc_call, maximum_log_block_range=50
    )

    observations = client.get_chainlink_observations("ETH/USD", start, end)

    assert [item.value for item in observations] == [Decimal("1"), Decimal("2")]
    assert [item.source for item in observations] == [
        "chainlink:ETH/USD",
        "chainlink:ETH/USD",
    ]
    assert {item["address"] for item in log_filters} == set(phase_addresses.values())
    assert all(int(item["fromBlock"], 16) >= 120 for item in log_filters)
    assert all(int(item["toBlock"], 16) <= 220 for item in log_filters)
    assert all(
        int(item["toBlock"], 16) - int(item["fromBlock"], 16) + 1 <= 50
        for item in log_filters
    )


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
