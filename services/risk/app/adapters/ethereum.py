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
DECIMALS_SELECTOR = "0x313ce567"
PHASE_ID_SELECTOR = "0x58303b10"
PHASE_AGGREGATORS_SELECTOR = "0xc1597304"
DEFAULT_MAXIMUM_LOG_BLOCK_RANGE = 10_000


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
        maximum_log_block_range: int = DEFAULT_MAXIMUM_LOG_BLOCK_RANGE,
    ) -> None:
        if maximum_log_block_range <= 0:
            raise ValueError("maximum log block range must be positive")
        self._rpc_url = settings.ethereum_rpc_url
        self._rpc_call = rpc_call or self._request
        self._maximum_log_block_range = maximum_log_block_range
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
        aggregators = self._phase_aggregators(definition)
        block_range = self._period_block_range(start, end)
        if block_range is None:
            return ()
        first_block, last_block = block_range
        raw_logs: list[Mapping[str, Any]] = []
        for aggregator in aggregators:
            for chunk_start in range(
                first_block,
                last_block + 1,
                self._maximum_log_block_range,
            ):
                chunk_end = min(
                    last_block,
                    chunk_start + self._maximum_log_block_range - 1,
                )
                chunk = self._call(
                    "eth_getLogs",
                    [
                        {
                            "address": aggregator,
                            "fromBlock": hex(chunk_start),
                            "toBlock": hex(chunk_end),
                            "topics": [ANSWER_UPDATED_TOPIC],
                        }
                    ],
                )
                if not isinstance(chunk, list) or not all(
                    isinstance(log, Mapping) for log in chunk
                ):
                    raise EthereumProviderError(
                        "Ethereum provider returned invalid log data"
                    )
                raw_logs.extend(chunk)

        observations = (
            _parse_answer_updated(log, decimals, definition.source)
            for log in raw_logs
            if isinstance(log, Mapping)
        )
        in_period = (
            observation
            for observation in observations
            if start.astimezone(UTC) <= observation.timestamp <= end.astimezone(UTC)
        )
        return _deduplicate_and_sort(in_period)

    def _phase_aggregators(self, definition: FeedDefinition) -> tuple[str, ...]:
        phase_count = _hex_integer(
            self._call(
                "eth_call",
                [{"to": definition.address, "data": PHASE_ID_SELECTOR}, "latest"],
            )
        )
        if phase_count <= 0 or phase_count > 65_535:
            raise EthereumProviderError("Ethereum provider returned invalid feed phases")
        return tuple(
            _address_from_word(
                self._call(
                    "eth_call",
                    [
                        {
                            "to": definition.address,
                            "data": _uint_call(PHASE_AGGREGATORS_SELECTOR, phase),
                        },
                        "latest",
                    ],
                )
            )
            for phase in range(1, phase_count + 1)
        )

    def _period_block_range(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[int, int] | None:
        latest = self.get_block_number()
        first = self._first_block_at_or_after(int(start.astimezone(UTC).timestamp()), latest)
        last = self._last_block_at_or_before(int(end.astimezone(UTC).timestamp()), latest)
        if first is None or last is None or first > last:
            return None
        return first, last

    def _first_block_at_or_after(self, timestamp: int, latest: int) -> int | None:
        if self._block_timestamp(latest) < timestamp:
            return None
        low = 0
        high = latest
        while low < high:
            middle = (low + high) // 2
            if self._block_timestamp(middle) < timestamp:
                low = middle + 1
            else:
                high = middle
        return low

    def _last_block_at_or_before(self, timestamp: int, latest: int) -> int | None:
        if self._block_timestamp(0) > timestamp:
            return None
        low = 0
        high = latest
        while low < high:
            middle = (low + high + 1) // 2
            if self._block_timestamp(middle) <= timestamp:
                low = middle
            else:
                high = middle - 1
        return low

    def _block_timestamp(self, block_number: int) -> int:
        block = self._call("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(block, Mapping):
            raise EthereumProviderError("Ethereum provider returned invalid block data")
        return _hex_integer(block.get("timestamp"))

    def _call(self, method: str, params: list[object]) -> object:
        try:
            return self._rpc_call(method, params)
        except Exception:
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
        except Exception:
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


def _parse_answer_updated(
    log: Mapping[str, Any],
    decimals: int,
    source: str,
) -> Observation:
    topics = log.get("topics")
    if (
        not isinstance(topics, list)
        or len(topics) < 3
        or not all(_is_word(topic) for topic in topics[:3])
    ):
        raise EthereumProviderError("Ethereum provider returned an invalid oracle log")
    if topics[0] != ANSWER_UPDATED_TOPIC:
        raise EthereumProviderError("Ethereum provider returned an unexpected oracle log")
    transaction_hash = log.get("transactionHash")
    if not _is_word(transaction_hash):
        raise EthereumProviderError("Ethereum provider returned an invalid oracle log")
    try:
        raw_answer = int.from_bytes(
            bytes.fromhex(str(topics[1])[2:]), "big", signed=True
        )
        timestamp = datetime.fromtimestamp(_hex_integer(log.get("data")), tz=UTC)
        block_number = _hex_integer(log.get("blockNumber"))
        log_index = _hex_integer(log.get("logIndex"))
    except Exception:
        raise EthereumProviderError(
            "Ethereum provider returned an invalid oracle log"
        ) from None
    return Observation(
        timestamp=timestamp,
        value=Decimal(raw_answer).scaleb(-decimals),
        block_number=block_number,
        log_index=log_index,
        transaction_hash=transaction_hash,
        source=source,
    )


def _deduplicate_and_sort(
    observations: Sequence[Observation] | Any,
) -> tuple[Observation, ...]:
    by_identity: dict[tuple[int, str, int], Observation] = {}
    for observation in observations:
        identity = (
            observation.block_number,
            observation.transaction_hash,
            observation.log_index,
        )
        current = by_identity.get(identity)
        if current is None or _observation_order(observation) > _observation_order(current):
            by_identity[identity] = observation
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.timestamp,
                item.block_number,
                item.transaction_hash,
                item.log_index,
            ),
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


def _uint_call(selector: str, value: int) -> str:
    return f"{selector}{value:064x}"


def _is_word(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True
