"""Ethereum JSON-RPC reads: chain state, Multicall3 batches, and Chainlink round history.

Chainlink history is read round by round, never by log scanning (free-tier RPC
keys cap `eth_getLogs` at a few blocks). An EACAggregatorProxy numbers its
rounds `(phaseId << 64) | aggregatorRoundId`; `latestRoundData()` gives the
newest round, and `getRoundData(roundId)` on the proxy returns any earlier
round of any phase. History is walked back from the latest round in batches of
`MULTICALL_BATCH_SIZE` `getRoundData` calls, each batch a single Multicall3
`aggregate3` `eth_call`. When a phase's first round is reached, the previous
phase's last round comes from `phaseAggregators(phase)` and that aggregator's
`latestRound()` (a direct `eth_call`: aggregators restrict reads to their proxy
or an externally owned caller, so it cannot go through Multicall3). The walk
stops at the first batch holding a round older than the requested start, so the
answer standing at the start is always known.

Rounds are cached per feed address (in memory, and in the SQLite cache when one
is supplied) with the contiguous range they cover. A repeat read costs one
`latestRoundData()` call plus the rounds published since, and extends the range
backwards only when an earlier start is requested.

Provider failures raise `EthereumProviderError` naming only the RPC method; the
RPC URL, request bodies and provider messages are never included.
"""

import json
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import httpx

from app.adapters.multicall import (
    MULTICALL3_ADDRESS,
    AbiDecodeError,
    Call,
    CallResult,
    call_data,
    decode_address,
    decode_aggregate3,
    decode_int,
    decode_uint,
    encode_aggregate3,
    hex_bytes,
)
from app.config import CachePolicy, Settings
from app.history.normalization import Observation


# Function selectors (first 4 bytes of keccak256 of the signature), computed
# offline and each verified by an eth_call against the mainnet ETH/USD proxy
# 0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419 or its phase aggregator.
DECIMALS_SELECTOR = "0x313ce567"  # decimals() -> 8
DESCRIPTION_SELECTOR = "0x7284e416"  # description() -> "ETH / USD"
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"  # latestRoundData()
GET_ROUND_DATA_SELECTOR = "0x9a6fc8f5"  # getRoundData(uint80)
PHASE_ID_SELECTOR = "0x58303b10"  # phaseId()
PHASE_AGGREGATORS_SELECTOR = "0xc1597304"  # phaseAggregators(uint16)
LATEST_ROUND_SELECTOR = "0x668a0f02"  # latestRound() on an aggregator

PHASE_OFFSET = 64
AGGREGATOR_ROUND_MASK = (1 << PHASE_OFFSET) - 1
# getRoundData calls per aggregate3 eth_call: 1000 proxy reads measured at
# 0.25 s and about 10 M gas, well inside provider eth_call limits.
MULTICALL_BATCH_SIZE = 1000
# A walk never reads more rounds than this (about 8 years of a 1-minute feed).
MAXIMUM_ROUNDS_PER_WALK = 4_000_000
ROUND_CACHE_NAMESPACE = "chainlink_rounds:v1"
ROUND_RETENTION = timedelta(days=400)
ROUND_CACHE_POLICY = CachePolicy(ttl=timedelta(days=1), retention=ROUND_RETENTION)
RPC_TIMEOUT_SECONDS = 20.0


class EthereumProviderError(RuntimeError):
    pass


class EthereumCallReverted(EthereumProviderError):
    """An `eth_call` executed and reverted (the contract has no such function or refused)."""


class UnsupportedFeedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeedDefinition:
    feed_id: str
    address: str
    source: str
    deployment_block: int | None = None
    description: str = ""
    note: str = ""
    components: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FeedRound:
    round_id: int
    answer: int
    updated_at: int

    @property
    def phase_id(self) -> int:
        return self.round_id >> PHASE_OFFSET


@dataclass
class _RoundHistory:
    decimals: int
    rounds: dict[int, FeedRound] = field(default_factory=dict)
    newest: int = 0
    oldest: int = 0
    exhausted: bool = False

    def earliest_update(self) -> int | None:
        return min((item.updated_at for item in self.rounds.values()), default=None)


RpcCall = Callable[[str, list[object]], object]


class EthereumClient:
    def __init__(
        self,
        settings: Settings,
        *,
        rpc_call: RpcCall | None = None,
        feed_catalog_path: str | Path | None = None,
        cache=None,
        batch_size: int = MULTICALL_BATCH_SIZE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch size must be positive")
        self._rpc_url = settings.ethereum_rpc_url
        self._rpc_call = rpc_call or self._request
        self._batch_size = batch_size
        self._cache = cache
        self._clock = clock
        path = Path(feed_catalog_path) if feed_catalog_path else _default_catalog_path()
        self._feeds = _load_feeds(path)
        self._histories: dict[str, _RoundHistory] = {}
        self._decimals: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # Chain state

    def get_chain_id(self) -> int:
        return _hex_integer(self._call("eth_chainId", []))

    def get_block_number(self) -> int:
        return _hex_integer(self._call("eth_blockNumber", []))

    def get_gas_price(self) -> Decimal:
        return Decimal(_hex_integer(self._call("eth_gasPrice", [])))

    # Calls

    def eth_call(self, to: str, data: str) -> bytes:
        result = self._call("eth_call", [{"to": to, "data": data}, "latest"])
        try:
            return hex_bytes(result)
        except AbiDecodeError:
            raise EthereumProviderError("Ethereum provider returned invalid call data") from None

    def aggregate(self, calls: Sequence[Call]) -> tuple[CallResult, ...]:
        """Run calls through Multicall3 `aggregate3`, `MULTICALL_BATCH_SIZE` per eth_call."""
        results: list[CallResult] = []
        for offset in range(0, len(calls), self._batch_size):
            chunk = calls[offset : offset + self._batch_size]
            payload = self.eth_call(MULTICALL3_ADDRESS, encode_aggregate3(chunk))
            try:
                decoded = decode_aggregate3(payload)
            except AbiDecodeError:
                raise EthereumProviderError(
                    "Ethereum provider returned an invalid multicall result"
                ) from None
            if len(decoded) != len(chunk):
                raise EthereumProviderError(
                    "Ethereum provider returned an incomplete multicall result"
                )
            results.extend(decoded)
        return tuple(results)

    # Feed catalog

    def feed(self, feed_id: str) -> FeedDefinition:
        definition = self._feeds.get(feed_id)
        if definition is None:
            raise UnsupportedFeedError(f"unsupported Chainlink feed: {feed_id}")
        return definition

    def feed_by_address(self, address: str) -> FeedDefinition | None:
        key = address.lower()
        return next(
            (item for item in self._feeds.values() if item.address and item.address.lower() == key),
            None,
        )

    def feed_notes(self) -> dict[str, str]:
        return {key: value.note for key, value in self._feeds.items() if value.note}

    # Chainlink reads

    def feed_decimals(self, address: str) -> int:
        key = address.lower()
        cached = self._decimals.get(key)
        if cached is not None:
            return cached
        try:
            decimals = decode_uint(self.eth_call(address, DECIMALS_SELECTOR))
        except AbiDecodeError:
            raise EthereumProviderError("Ethereum provider returned invalid feed decimals") from None
        if decimals > 77:
            raise EthereumProviderError("Ethereum provider returned invalid feed decimals")
        self._decimals[key] = decimals
        return decimals

    def latest_round(self, address: str) -> FeedRound:
        """`latestRoundData()` of a feed; a round id or update time of zero is returned as is."""
        return _decode_round(self.eth_call(address, LATEST_ROUND_DATA_SELECTOR))

    def get_latest_observation(self, feed: str) -> Observation | None:
        """The feed's current answer from `latestRoundData()` (one call per component)."""
        definition = self.feed(feed)
        if definition.components:
            parts = [self.get_latest_observation(component) for component in definition.components]
            if any(part is None for part in parts):
                return None
            present = [part for part in parts if part is not None]
            return Observation(
                timestamp=min(part.timestamp for part in present),
                value=_product(part.value for part in present),
                block_number=0,
                log_index=0,
                source=definition.source,
            )
        latest = self.latest_round(definition.address)
        if latest.round_id <= 0 or latest.updated_at <= 0:
            return None
        return _observation(latest, self.feed_decimals(definition.address), definition.source)

    def get_chainlink_observations(
        self,
        feed: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Observation, ...]:
        """Every answer of a catalog feed updated in [start, end], oldest first."""
        _validate_period(start, end)
        definition = self.feed(feed)
        lower = int(start.astimezone(UTC).timestamp())
        upper = int(end.astimezone(UTC).timestamp())
        if definition.components:
            return _composite(
                [
                    self.get_feed_updates(self.feed(component).address, start, end)
                    for component in definition.components
                ],
                lower,
                upper,
                definition.source,
            )
        updates = self.get_feed_updates(definition.address, start, end, source=definition.source)
        return tuple(
            item for item in updates if lower <= int(item.timestamp.timestamp()) <= upper
        )

    def get_feed_updates(
        self,
        address: str,
        start: datetime,
        end: datetime,
        *,
        source: str | None = None,
    ) -> tuple[Observation, ...]:
        """Answers of any Chainlink proxy updated in [start, end], plus the last one before start.

        The extra leading answer is the one standing at `start`, so update gaps
        that straddle the start are measured exactly. Raises
        `UnsupportedFeedError` for a contract that is not a Chainlink aggregator
        proxy with readable rounds.
        """
        _validate_period(start, end)
        lower = int(start.astimezone(UTC).timestamp())
        upper = int(end.astimezone(UTC).timestamp())
        known = self.feed_by_address(address)
        label = source or (known.source if known else f"chainlink:{address.lower()}")
        history = self._history(address, lower)
        ordered = sorted(history.rounds.values(), key=lambda item: item.round_id)
        before = [item for item in ordered if item.updated_at < lower]
        inside = [item for item in ordered if lower <= item.updated_at <= upper]
        chosen = ([before[-1]] if before else []) + inside
        return tuple(_observation(item, history.decimals, label) for item in chosen)

    # Round history

    def _history(self, address: str, start: int) -> _RoundHistory:
        key = address.lower()
        with self._lock_for(key):
            history = self._histories.get(key) or self._load(key)
            before = _signature(history)
            latest = self.latest_round(address)
            if latest.round_id <= 0 or latest.updated_at <= 0 or latest.phase_id <= 0:
                raise UnsupportedFeedError("feed has no Chainlink round history")
            if history is None:
                history = _RoundHistory(decimals=self.feed_decimals(address))
                rounds, exhausted = self._walk_back(address, latest.round_id, start=start)
                _merge(history, rounds)
                history.newest = latest.round_id
                history.oldest = min(history.rounds, default=latest.round_id)
                history.exhausted = exhausted
            elif latest.round_id > history.newest:
                rounds, _ = self._walk_back(address, latest.round_id, after=history.newest)
                _merge(history, rounds)
                history.newest = latest.round_id
            earliest = history.earliest_update()
            if not history.exhausted and (earliest is None or earliest >= start):
                previous = self._previous_round(address, history.oldest)
                if previous is None:
                    history.exhausted = True
                else:
                    rounds, exhausted = self._walk_back(address, previous, start=start)
                    _merge(history, rounds)
                    history.oldest = min(history.rounds, default=history.oldest)
                    history.exhausted = exhausted
            self._prune(history)
            self._histories[key] = history
            if _signature(history) != before:
                self._save(key, history)
            return history

    def _walk_back(
        self,
        address: str,
        from_round: int,
        *,
        start: int | None = None,
        after: int | None = None,
    ) -> tuple[list[FeedRound], bool]:
        """Rounds from `from_round` backwards until one predates `start` or `after` is reached.

        Returns the rounds and whether the feed's first round was reached.
        """
        phase = from_round >> PHASE_OFFSET
        upper = from_round & AGGREGATOR_ROUND_MASK
        collected: list[FeedRound] = []
        read = 0
        while True:
            lower = max(1, upper - self._batch_size + 1)
            if after is not None and after >> PHASE_OFFSET == phase:
                lower = max(lower, (after & AGGREGATOR_ROUND_MASK) + 1)
            if lower <= upper:
                identifiers = [(phase << PHASE_OFFSET) | item for item in range(upper, lower - 1, -1)]
                batch = self._read_rounds(address, identifiers)
                collected.extend(batch)
                read += len(identifiers)
                if start is not None and any(item.updated_at < start for item in batch):
                    return collected, False
            if after is not None and (phase << PHASE_OFFSET) | lower <= after + 1:
                return collected, False
            if read >= MAXIMUM_ROUNDS_PER_WALK:
                raise EthereumProviderError("Chainlink round history exceeds the walk limit")
            if lower > 1:
                upper = lower - 1
                continue
            previous = self._previous_round(address, (phase << PHASE_OFFSET) | 1)
            if previous is None:
                return collected, True
            if after is not None and previous <= after:
                return collected, False
            phase = previous >> PHASE_OFFSET
            upper = previous & AGGREGATOR_ROUND_MASK

    def _previous_round(self, address: str, round_id: int) -> int | None:
        """The round id before `round_id`, crossing into earlier phases; None at the first round."""
        phase = round_id >> PHASE_OFFSET
        aggregator_round = round_id & AGGREGATOR_ROUND_MASK
        if aggregator_round > 1:
            return round_id - 1
        for earlier in range(phase - 1, 0, -1):
            last = self._phase_last_round(address, earlier)
            if last:
                return (earlier << PHASE_OFFSET) | last
        return None

    def _phase_last_round(self, address: str, phase: int) -> int | None:
        try:
            aggregator = decode_address(
                self.eth_call(address, call_data(PHASE_AGGREGATORS_SELECTOR, phase))
            )
        except (EthereumCallReverted, AbiDecodeError):
            return None
        if int(aggregator, 16) == 0:
            return None
        try:
            return decode_uint(self.eth_call(aggregator, LATEST_ROUND_SELECTOR)) & AGGREGATOR_ROUND_MASK
        except (EthereumCallReverted, AbiDecodeError):
            return None

    def _read_rounds(self, address: str, identifiers: Sequence[int]) -> list[FeedRound]:
        calls = [Call(address, call_data(GET_ROUND_DATA_SELECTOR, item)) for item in identifiers]
        rounds: list[FeedRound] = []
        for identifier, result in zip(identifiers, self.aggregate(calls), strict=True):
            if not result.success or len(result.data) < 160:
                continue  # a missing round reverts or is empty on older aggregators
            try:
                item = _decode_round(result.data)
            except EthereumProviderError:
                continue
            if item.updated_at > 0:
                rounds.append(FeedRound(identifier, item.answer, item.updated_at))
        return rounds

    def _prune(self, history: _RoundHistory) -> None:
        cutoff = int((self._clock() - ROUND_RETENTION).timestamp())
        old = sorted(
            (item for item in history.rounds.values() if item.updated_at < cutoff),
            key=lambda item: item.round_id,
        )
        if len(old) <= 1:
            return
        for item in old[:-1]:
            del history.rounds[item.round_id]
        history.oldest = min(history.rounds)
        history.exhausted = False

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _load(self, key: str) -> _RoundHistory | None:
        if self._cache is None:
            return None
        try:
            cached = self._cache.get(ROUND_CACHE_NAMESPACE, key, self._clock())
            if cached is None:
                return None
            payload = json.loads(cached.payload)
            history = _RoundHistory(
                decimals=int(payload["decimals"]),
                newest=int(payload["newest"]),
                oldest=int(payload["oldest"]),
                exhausted=bool(payload["exhausted"]),
            )
            for round_id, answer, updated_at in payload["rounds"]:
                history.rounds[int(round_id)] = FeedRound(int(round_id), int(answer), int(updated_at))
        except Exception:
            return None  # an unreadable cache entry is refetched
        self._decimals.setdefault(key, history.decimals)
        return history

    def _save(self, key: str, history: _RoundHistory) -> None:
        if self._cache is None:
            return
        payload = {
            "decimals": history.decimals,
            "newest": history.newest,
            "oldest": history.oldest,
            "exhausted": history.exhausted,
            "rounds": [
                [item.round_id, item.answer, item.updated_at]
                for item in sorted(history.rounds.values(), key=lambda value: value.round_id)
            ],
        }
        self._cache.set(
            ROUND_CACHE_NAMESPACE,
            key,
            json.dumps(payload, separators=(",", ":")).encode(),
            self._clock(),
            ROUND_CACHE_POLICY,
        )

    # Transport

    def _call(self, method: str, params: list[object]) -> object:
        try:
            return self._rpc_call(method, params)
        except EthereumCallReverted:
            raise EthereumCallReverted(f"Ethereum call reverted for {method}") from None
        except Exception:
            raise EthereumProviderError(
                f"Ethereum provider request failed for {method}"
            ) from None

    def _request(self, method: str, params: list[object]) -> object:
        try:
            response = httpx.post(
                self._rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=RPC_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            raise EthereumProviderError(
                f"Ethereum provider request failed for {method}"
            ) from None
        if isinstance(payload, dict) and _is_revert(payload.get("error")):
            raise EthereumCallReverted(f"Ethereum call reverted for {method}")
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
    feeds: dict[str, FeedDefinition] = {}
    for item in payload.get("feeds", []):
        components = tuple(item.get("components", ()))
        address = item.get("address", "")
        if bool(components) == bool(address):
            raise ValueError(f"feed {item.get('id')} needs exactly one of address or components")
        definition = FeedDefinition(
            feed_id=item["id"],
            address=address,
            source=item["source"],
            deployment_block=(
                int(item["deployment_block"]) if item.get("deployment_block") is not None else None
            ),
            description=item.get("description", ""),
            note=item.get("note", ""),
            components=components,
        )
        feeds[definition.feed_id] = definition
    for definition in feeds.values():
        for component in definition.components:
            if component not in feeds or feeds[component].components:
                raise ValueError(f"feed {definition.feed_id} has an unknown component {component}")
    return feeds


def _decode_round(data: bytes) -> FeedRound:
    try:
        return FeedRound(
            round_id=decode_uint(data, 0),
            answer=decode_int(data, 1),
            updated_at=decode_uint(data, 3),
        )
    except AbiDecodeError:
        raise EthereumProviderError("Ethereum provider returned invalid round data") from None


def _observation(item: FeedRound, decimals: int, source: str) -> Observation:
    return Observation(
        timestamp=datetime.fromtimestamp(item.updated_at, tz=UTC),
        value=Decimal(item.answer).scaleb(-decimals),
        block_number=0,
        log_index=0,
        source=source,
        round_id=item.round_id,
    )


def _composite(
    components: Sequence[Sequence[Observation]], lower: int, upper: int, source: str
) -> tuple[Observation, ...]:
    """Product of the components' standing answers at every update time in [lower, upper]."""
    events = sorted(
        (int(item.timestamp.timestamp()), index, item.value)
        for index, updates in enumerate(components)
        for item in updates
    )
    standing: dict[int, Decimal] = {}
    by_time: dict[int, Decimal] = {}
    for timestamp, index, value in events:
        standing[index] = value
        if len(standing) == len(components) and lower <= timestamp <= upper:
            by_time[timestamp] = _product(standing.values())
    return tuple(
        Observation(
            timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
            value=value,
            block_number=0,
            log_index=0,
            source=source,
            round_id=timestamp,
        )
        for timestamp, value in sorted(by_time.items())
    )


def _product(values: Iterable[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        result = Decimal(1)
        for value in values:
            result *= value
        return +result


def _signature(history: _RoundHistory | None) -> tuple[int, int, int, bool] | None:
    """What a save would change: covered range, size and exhaustion."""
    if history is None:
        return None
    return history.newest, history.oldest, len(history.rounds), history.exhausted


def _merge(history: _RoundHistory, rounds: Iterable[FeedRound]) -> None:
    for item in rounds:
        history.rounds[item.round_id] = item


def _is_revert(error: object) -> bool:
    if not isinstance(error, Mapping):
        return False
    message = str(error.get("message", "")).lower()
    return error.get("code") == 3 or "revert" in message


def _hex_integer(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthereumProviderError("Ethereum provider returned an invalid hexadecimal value")
    try:
        return int(value, 16)
    except ValueError:
        raise EthereumProviderError(
            "Ethereum provider returned an invalid hexadecimal value"
        ) from None


def _validate_period(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end < start:
        raise ValueError("end must not precede start")
