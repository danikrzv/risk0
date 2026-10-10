import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.history.normalization import Observation


ANSWER_UPDATED_TOPIC = (
    "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
)
AGGREGATOR_SELECTOR = "0x245a7bfc"
DECIMALS_SELECTOR = "0x313ce567"


class EthereumProviderError(RuntimeError):
    pass


class UnsupportedFeedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeedDefinition:
    feed_id: str
    address: str
    deployment_block: int
    source: str


RpcCall = Callable[[str, list[object]], object]


class EthereumClient:
    def __init__(
        self,
        settings: Settings,
        *,
        rpc_call: RpcCall | None = None,
        feed_catalog_path: str | Path | None = None,
    ) -> None:
        self._rpc_url = settings.ethereum_rpc_url
        self._rpc_call = rpc_call or self._request
        path = Path(feed_catalog_path) if feed_catalog_path else _default_catalog_path()
        self._feeds = _load_feeds(path)

    def get_block_number(self) -> int:
        return _hex_integer(self._call("eth_blockNumber", []))

    def get_gas_price(self) -> Decimal:
        return Decimal(_hex_integer(self._call("eth_gasPrice", [])))

    def get_chainlink_observations(
        self,
        feed: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Observation, ...]:
        _validate_period(start, end)
        definition = self._feeds.get(feed)
        if definition is None:
            raise UnsupportedFeedError(f"unsupported Chainlink feed: {feed}")

        decimals = _hex_integer(
            self._call(
                "eth_call",
                [{"to": definition.address, "data": DECIMALS_SELECTOR}, "latest"],
            )
        )
        aggregator = _address_from_word(
            self._call(
                "eth_call",
                [{"to": definition.address, "data": AGGREGATOR_SELECTOR}, "latest"],
            )
        )
        raw_logs = self._call(
            "eth_getLogs",
            [
                {
                    "address": aggregator,
                    "fromBlock": hex(definition.deployment_block),
                    "toBlock": "latest",
                    "topics": [ANSWER_UPDATED_TOPIC],
                }
            ],
        )
        if not isinstance(raw_logs, list):
            raise EthereumProviderError("Ethereum provider returned invalid log data")

        observations = (
            _parse_answer_updated(log, decimals)
            for log in raw_logs
            if isinstance(log, Mapping)
        )
        in_period = (
            observation
            for observation in observations
            if start.astimezone(UTC) <= observation.timestamp <= end.astimezone(UTC)
        )
        return _deduplicate_and_sort(in_period)

    def _call(self, method: str, params: list[object]) -> object:
        try:
            return self._rpc_call(method, params)
        except EthereumProviderError:
            raise
        except Exception as error:
            raise EthereumProviderError(
                f"Ethereum provider request failed for {method}"
            ) from None

    def _request(self, method: str, params: list[object]) -> object:
        try:
            response = httpx.post(
                self._rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise EthereumProviderError(
                f"Ethereum provider request failed for {method}"
            ) from None
        if not isinstance(payload, dict) or "result" not in payload:
            raise EthereumProviderError(
                f"Ethereum provider returned an invalid response for {method}"
            )
        return payload["result"]


def _default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "catalog" / "feeds.ethereum.json"


def _load_feeds(path: Path) -> dict[str, FeedDefinition]:
    payload = json.loads(path.read_text())
    if payload.get("chain_id") != 1:
        raise ValueError("Ethereum feed catalog must target chain ID 1")
    feeds = {}
    for item in payload.get("feeds", []):
        definition = FeedDefinition(
            feed_id=item["id"],
            address=item["address"],
            deployment_block=int(item["deployment_block"]),
            source=item["source"],
        )
        feeds[definition.feed_id] = definition
    return feeds


def _parse_answer_updated(log: Mapping[str, Any], decimals: int) -> Observation:
    topics = log.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 3:
        raise EthereumProviderError("Ethereum provider returned an invalid oracle log")
    if topics[0] != ANSWER_UPDATED_TOPIC:
        raise EthereumProviderError("Ethereum provider returned an unexpected oracle log")
    raw_answer = int.from_bytes(bytes.fromhex(str(topics[1])[2:]), "big", signed=True)
    timestamp = datetime.fromtimestamp(_hex_integer(log.get("data")), tz=UTC)
    return Observation(
        timestamp=timestamp,
        value=Decimal(raw_answer).scaleb(-decimals),
        block_number=_hex_integer(log.get("blockNumber")),
        log_index=_hex_integer(log.get("logIndex")),
        transaction_hash=str(log.get("transactionHash", "")),
    )


def _deduplicate_and_sort(
    observations: Sequence[Observation] | Any,
) -> tuple[Observation, ...]:
    by_block: dict[int, Observation] = {}
    for observation in observations:
        current = by_block.get(observation.block_number)
        if current is None or _observation_order(observation) > _observation_order(current):
            by_block[observation.block_number] = observation
    return tuple(
        sorted(
            by_block.values(),
            key=lambda item: (item.timestamp, item.block_number, item.log_index),
        )
    )


def _observation_order(observation: Observation) -> tuple[int, str, datetime, Decimal]:
    return (
        observation.log_index,
        observation.transaction_hash,
        observation.timestamp,
        observation.value,
    )


def _hex_integer(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthereumProviderError("Ethereum provider returned an invalid hexadecimal value")
    try:
        return int(value, 16)
    except ValueError as error:
        raise EthereumProviderError(
            "Ethereum provider returned an invalid hexadecimal value"
        ) from None


def _address_from_word(value: object) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise EthereumProviderError("Ethereum provider returned an invalid address")
    try:
        int(value[2:], 16)
    except ValueError:
        raise EthereumProviderError(
            "Ethereum provider returned an invalid address"
        ) from None
    address = value[-40:].lower()
    if int(address, 16) == 0:
        raise EthereumProviderError("Ethereum provider returned an invalid address")
    return f"0x{address}"


def _validate_period(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end < start:
        raise ValueError("end must not precede start")
