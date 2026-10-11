"""Decode Morpho market oracles into the price feeds they read.

Morpho Blue markets price collateral with an immutable oracle contract. The
standard factory oracles are `MorphoChainlinkOracleV2` (getters `BASE_FEED_1`,
`BASE_FEED_2`, `QUOTE_FEED_1`, `QUOTE_FEED_2`, `BASE_VAULT`, `QUOTE_VAULT`,
`SCALE_FACTOR`) and its predecessor `MorphoChainlinkOracle` (the same feeds and
scale factor with a single `VAULT`). The price is

    SCALE_FACTOR * base vault rate * BASE_FEED_1 * BASE_FEED_2
                 / (quote vault rate * QUOTE_FEED_1 * QUOTE_FEED_2)

and `price()` returns it as the collateral price in loan-token units scaled by
1e36 (adjusted for token decimals). An unused feed or vault is the zero address.

A decoded feed is classified by what it does on-chain, never by its name alone.
The rules are applied in this order; the first that holds wins:

- `chainlink`: answers `phaseId()` (> 0) and a `latestRoundData()` with a
  non-zero round id and update time, so it has a heartbeat and a round history.
- `exchange_rate_adapter`: one of the documented on-chain rate adapters below
  (address and `description()` both match). They compute the answer when read,
  so they cannot be stale; their risk is the rate source itself.
- `constant_scaler`: a computed-on-read feed (next rule) whose answer is an
  exact power of ten, identical at both blocks read, returned unchanged by
  `getRoundData` for a round id that cannot exist yet (latest + 1), and whose
  description marks it as a scaler ("dummy", "constant", "fixed" or "scal").
  Morpho oracles use such feeds only to scale decimals ("Dummy feed with 12
  decimals"): no price risk and nothing to go stale. The description is a
  necessary, not a sufficient, condition, and it only separates two kinds that
  are both treated as never stale: a live rate that happens to read exactly 1.0
  (InfiniFi's RT oracle, 1e18 at 18 decimals) is not described as a scaler, so
  it stays `computed_on_read`.
- `computed_on_read`: the answer is produced when the feed is called rather
  than stored by an updater. Rule: on two reads at consecutive blocks (latest
  and latest - 1, one Multicall3 call each, with the block timestamp read in
  the same call), `updatedAt` is 0 on both with a positive answer, or equals
  that block's own timestamp on both. A push feed would have to be updated in
  both blocks to match. Such a feed cannot be stale (its time is always "now");
  its risk is whatever it computes from, which this classification does not
  observe (an adapter that hides a stale upstream feed behind `updatedAt` 0 is
  not detected).
- `third_party`: any other AggregatorV3-compatible feed: a non-empty
  `description()`, `decimals()`, and a `latestRoundData()` with a positive
  answer and an update time in (0, block time], but no Chainlink phase. Its age
  is observable; its heartbeat is known only when published
  (`ThirdPartyFeedRegistry`) or inferable from its round history. `provider` is
  parsed from the description for display only (RedStone, Ojo, Midas,
  Chronicle, Pyth, API3, Custom, otherwise unknown). `history_readable` is set
  when `getRoundData(latest - 1)` returns an earlier round with an update time.
- `unknown`: anything else. An oracle with an unknown feed is not reported as
  healthy: the health provider treats it as unobservable.

Selectors were computed offline (keccak256) and verified with eth_call on the
WETH/USDC 86% market oracle 0x0f948cba8231db7898ef36a4212581ad7b1b4580 (V2:
BASE_FEED_1 = an ETH / USD proxy, SCALE_FACTOR = 1e16, price() = 2508.34e24 on
2026-10-11) and the WBTC/USDC 86% oracle 0xdddd770badd886df3864029e4b377b5f6a2b6b83
(V1: VAULT = zero, BASE_VAULT reverts).
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.adapters.ethereum import (
    DECIMALS_SELECTOR,
    DESCRIPTION_SELECTOR,
    GET_ROUND_DATA_SELECTOR,
    LATEST_ROUND_DATA_SELECTOR,
    PHASE_ID_SELECTOR,
    FeedRound,
)
from app.adapters.multicall import (
    GET_BLOCK_NUMBER_SELECTOR,
    GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR,
    MULTICALL3_ADDRESS,
    AbiDecodeError,
    Call,
    CallResult,
    call_data,
    decode_address,
    decode_int,
    decode_string,
    decode_uint,
)


BASE_FEED_1_SELECTOR = "0xf50a4718"  # BASE_FEED_1()
BASE_FEED_2_SELECTOR = "0xdc53858c"  # BASE_FEED_2()
QUOTE_FEED_1_SELECTOR = "0x56095e11"  # QUOTE_FEED_1()
QUOTE_FEED_2_SELECTOR = "0xacfbd39e"  # QUOTE_FEED_2()
BASE_VAULT_SELECTOR = "0xeaa2d7b4"  # BASE_VAULT() (V2)
QUOTE_VAULT_SELECTOR = "0x2e6f20a6"  # QUOTE_VAULT() (V2)
VAULT_SELECTOR = "0x411557d1"  # VAULT() (V1)
SCALE_FACTOR_SELECTOR = "0xce4b5bbe"  # SCALE_FACTOR()
PRICE_SELECTOR = "0xa035b1fe"  # price()

ORACLE_V2 = "MorphoChainlinkOracleV2"
ORACLE_V1 = "MorphoChainlinkOracle"
FEED_ROLES = ("BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2")
ZERO_ADDRESS = "0x" + "0" * 40

# On-chain exchange-rate adapters that implement the Chainlink interface but
# compute the answer when read. Each address was verified by description() and
# by comparing its answer with the rate source (wstETH.stEthPerToken() returned
# the same 1.246013674805236042 on 2026-10-11).
KNOWN_RATE_ADAPTERS: dict[str, str] = {
    "0x905b7dabcd3ce6b792d874e303d336424cdb1421": "wstETH/stETH exchange rate",
}

# Description markers a constant scaler must carry (besides the behavioural rules).
SCALER_MARKERS = ("dummy", "constant", "fixed", "scal")
# Display-only provider names parsed from a feed description, in match order.
PROVIDER_MARKERS: tuple[tuple[str, str], ...] = (
    ("redstone", "RedStone"),
    ("ojo", "Ojo"),
    ("midas", "Midas"),
    ("mglo", "Midas"),
    ("chronicle", "Chronicle"),
    ("pyth", "Pyth"),
    ("api3", "API3"),
    ("custom", "Custom"),
)
UNKNOWN_PROVIDER = "unknown"
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


class FeedKind(StrEnum):
    CHAINLINK = "chainlink"
    EXCHANGE_RATE_ADAPTER = "exchange_rate_adapter"
    COMPUTED_ON_READ = "computed_on_read"
    CONSTANT_SCALER = "constant_scaler"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


# Kinds whose answer is produced when read: there is no update time to go stale.
NEVER_STALE_KINDS = frozenset(
    {FeedKind.EXCHANGE_RATE_ADAPTER, FeedKind.COMPUTED_ON_READ, FeedKind.CONSTANT_SCALER}
)


@dataclass(frozen=True, slots=True)
class OracleFeed:
    role: str
    address: str
    kind: FeedKind
    description: str | None
    decimals: int | None
    provider: str | None = None
    history_readable: bool = False
    constant_answer: int | None = None


@dataclass(frozen=True, slots=True)
class MorphoOracle:
    address: str
    oracle_type: str
    feeds: tuple[OracleFeed, ...]
    base_vault: str | None
    quote_vault: str | None
    scale_factor: int

    @property
    def decodable(self) -> bool:
        return all(feed.kind is not FeedKind.UNKNOWN for feed in self.feeds)


@dataclass(frozen=True, slots=True)
class OracleState:
    price: int | None
    latest: tuple[FeedRound | None, ...]
    block_timestamp: int | None = None


class Aggregator(Protocol):
    """The slice of `EthereumClient` this module uses."""

    def aggregate(
        self, calls: Sequence[Call], *, block: int | None = None
    ) -> tuple[CallResult, ...]: ...


class MorphoOracleReader:
    def __init__(self, client: Aggregator) -> None:
        self._client = client

    def decode(self, oracle_address: str) -> MorphoOracle | None:
        """The oracle's configuration, or None when it is not a Morpho Chainlink oracle."""
        address = oracle_address.lower()
        selectors = (
            BASE_FEED_1_SELECTOR,
            BASE_FEED_2_SELECTOR,
            QUOTE_FEED_1_SELECTOR,
            QUOTE_FEED_2_SELECTOR,
            SCALE_FACTOR_SELECTOR,
            BASE_VAULT_SELECTOR,
            QUOTE_VAULT_SELECTOR,
            VAULT_SELECTOR,
        )
        results = self._client.aggregate([Call(address, selector) for selector in selectors])
        feeds = [_address(result) for result in results[:4]]
        scale = _uint(results[4])
        if any(item is None for item in feeds) or scale is None or scale == 0:
            return None
        base_vault, quote_vault, vault = (_address(result) for result in results[5:8])
        if base_vault is not None and quote_vault is not None:
            oracle_type = ORACLE_V2
        elif vault is not None:
            oracle_type, base_vault, quote_vault = ORACLE_V1, vault, ZERO_ADDRESS
        else:
            return None
        used = [
            (role, feed)
            for role, feed in zip(FEED_ROLES, feeds, strict=True)
            if feed is not None and feed != ZERO_ADDRESS
        ]
        return MorphoOracle(
            address=address,
            oracle_type=oracle_type,
            feeds=self._classify(used),
            base_vault=None if base_vault == ZERO_ADDRESS else base_vault,
            quote_vault=None if quote_vault == ZERO_ADDRESS else quote_vault,
            scale_factor=scale,
        )

    def read_state(self, oracle: MorphoOracle) -> OracleState:
        """`price()`, every feed's `latestRoundData()` and the block time in one multicall."""
        calls = [Call(oracle.address, PRICE_SELECTOR)]
        calls.extend(Call(feed.address, LATEST_ROUND_DATA_SELECTOR) for feed in oracle.feeds)
        calls.append(Call(MULTICALL3_ADDRESS, GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR))
        results = self._client.aggregate(calls)
        return OracleState(
            price=_uint(results[0]),
            latest=tuple(_round(result) for result in results[1:-1]),
            block_timestamp=_uint(results[-1]),
        )

    def _classify(self, used: Sequence[tuple[str, str]]) -> tuple[OracleFeed, ...]:
        if not used:
            return ()
        calls = [
            Call(MULTICALL3_ADDRESS, GET_BLOCK_NUMBER_SELECTOR),
            Call(MULTICALL3_ADDRESS, GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR),
        ]
        for _, feed in used:
            calls.extend(
                Call(feed, selector)
                for selector in (
                    DESCRIPTION_SELECTOR,
                    DECIMALS_SELECTOR,
                    PHASE_ID_SELECTOR,
                    LATEST_ROUND_DATA_SELECTOR,
                )
            )
        results = self._client.aggregate(calls)
        block, block_time = _uint(results[0]), _uint(results[1])
        reads: list[_FeedRead] = []
        for index, (role, feed) in enumerate(used):
            description, decimals, phase, latest = results[2 + 4 * index : 6 + 4 * index]
            places = _uint(decimals)
            reads.append(
                _FeedRead(
                    role=role,
                    address=feed,
                    description=_string(description),
                    decimals=places if places is not None and places <= 77 else None,
                    phase=_uint(phase),
                    latest=_round(latest),
                )
            )
        confirmations = self._confirm(reads, block)
        return tuple(_feed(item, block_time, confirmations.get(item.address)) for item in reads)

    def _confirm(self, reads: Sequence["_FeedRead"], block: int | None) -> dict[str, "_Confirmation"]:
        """A second read at the previous block for every feed a later rule needs it for.

        One multicall at `block - 1`: the block timestamp, then per candidate its
        `latestRoundData()`, `getRoundData(latest - 1)` (history probe) and
        `getRoundData(latest + 1)` (a round that cannot exist yet).
        """
        candidates = [item for item in reads if item.needs_confirmation]
        if not candidates or block is None or block <= 1:
            return {}
        calls = [Call(MULTICALL3_ADDRESS, GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR)]
        for item in candidates:
            round_id = item.latest.round_id if item.latest is not None else 0
            calls.append(Call(item.address, LATEST_ROUND_DATA_SELECTOR))
            calls.append(Call(item.address, call_data(GET_ROUND_DATA_SELECTOR, max(0, round_id - 1))))
            calls.append(Call(item.address, call_data(GET_ROUND_DATA_SELECTOR, round_id + 1)))
        results = self._client.aggregate(calls, block=block - 1)
        block_time = _uint(results[0])
        confirmations: dict[str, _Confirmation] = {}
        for index, item in enumerate(candidates):
            latest, previous, future = results[1 + 3 * index : 4 + 3 * index]
            confirmations[item.address] = _Confirmation(
                block_time=block_time,
                latest=_round(latest),
                previous=_round(previous),
                future=_round(future),
            )
        return confirmations


@dataclass(frozen=True, slots=True)
class _FeedRead:
    role: str
    address: str
    description: str | None
    decimals: int | None
    phase: int | None
    latest: FeedRound | None

    @property
    def is_chainlink(self) -> bool:
        latest = self.latest
        return (
            self.phase is not None
            and self.phase > 0
            and self.decimals is not None
            and latest is not None
            and latest.round_id > 0
            and latest.updated_at > 0
        )

    @property
    def is_rate_adapter(self) -> bool:
        documented = KNOWN_RATE_ADAPTERS.get(self.address)
        return (
            documented is not None
            and documented == self.description
            and self.decimals is not None
            and self.latest is not None
        )

    @property
    def needs_confirmation(self) -> bool:
        return (
            not self.is_chainlink
            and not self.is_rate_adapter
            and self.decimals is not None
            and self.latest is not None
            and self.latest.answer > 0
        )


@dataclass(frozen=True, slots=True)
class _Confirmation:
    block_time: int | None
    latest: FeedRound | None
    previous: FeedRound | None
    future: FeedRound | None


def _feed(read: _FeedRead, block_time: int | None, confirmation: "_Confirmation | None") -> OracleFeed:
    def make(kind: FeedKind, **extra: object) -> OracleFeed:
        return OracleFeed(read.role, read.address, kind, read.description, read.decimals, **extra)  # type: ignore[arg-type]

    if read.is_chainlink:
        return make(FeedKind.CHAINLINK)
    if read.is_rate_adapter:
        return make(FeedKind.EXCHANGE_RATE_ADAPTER)
    latest = read.latest
    if not read.needs_confirmation or latest is None:
        return make(FeedKind.UNKNOWN)
    provider = parse_provider(read.description)
    if confirmation is not None and _computed_on_read(latest, block_time, confirmation):
        if _constant_scaler(read, latest, confirmation):
            return make(FeedKind.CONSTANT_SCALER, provider=provider, constant_answer=latest.answer)
        return make(FeedKind.COMPUTED_ON_READ, provider=provider)
    if (
        read.description is not None
        and read.description.strip()
        and latest.updated_at > 0
        and (block_time is None or latest.updated_at <= block_time)
    ):
        previous = confirmation.previous if confirmation is not None else None
        readable = (
            latest.round_id > 0
            and previous is not None
            and 0 < previous.updated_at <= latest.updated_at
        )
        return make(FeedKind.THIRD_PARTY, provider=provider, history_readable=readable)
    return make(FeedKind.UNKNOWN, provider=provider)


def _computed_on_read(latest: FeedRound, block_time: int | None, confirmation: _Confirmation) -> bool:
    second = confirmation.latest
    if second is None or second.answer <= 0:
        return False
    if latest.updated_at == 0 and second.updated_at == 0:
        return True
    return (
        block_time is not None
        and confirmation.block_time is not None
        and confirmation.block_time != block_time
        and latest.updated_at == block_time
        and second.updated_at == confirmation.block_time
    )


def _constant_scaler(read: _FeedRead, latest: FeedRound, confirmation: _Confirmation) -> bool:
    description = (read.description or "").casefold()
    second, future = confirmation.latest, confirmation.future
    return (
        any(marker in description for marker in SCALER_MARKERS)
        and is_power_of_ten(latest.answer)
        and second is not None
        and second.answer == latest.answer
        and future is not None
        and future.answer == latest.answer
    )


def is_power_of_ten(value: int) -> bool:
    if value < 1:
        return False
    while value % 10 == 0:
        value //= 10
    return value == 1


def parse_provider(description: str | None) -> str:
    """Display-only provider name from a feed description; never used for a decision."""
    text = (description or "").casefold()
    return next((name for marker, name in PROVIDER_MARKERS if marker in text), UNKNOWN_PROVIDER)


# Published heartbeats of third-party feeds


@dataclass(frozen=True, slots=True)
class PublishedHeartbeat:
    address: str
    description: str
    provider: str
    heartbeat_seconds: int
    source_url: str


class ThirdPartyFeedRegistry:
    """Version-controlled published heartbeats of non-Chainlink feeds.

    `data/catalog/oracle-feeds.third-party.ethereum.json` holds an entry only
    where the provider (or the documented upstream feed it passes through)
    publishes the feed's heartbeat, with its source URL; nothing is inferred
    there. An entry applies only when the feed's on-chain `description()`
    equals the registered one, so a wrong address cannot lend a heartbeat to
    another feed.
    """

    def __init__(self, entries: Mapping[str, PublishedHeartbeat] | None = None) -> None:
        self._entries = dict(entries or {})

    @classmethod
    def load(cls, path: Path | None = None) -> "ThirdPartyFeedRegistry":
        source = path or default_third_party_registry_path()
        payload = json.loads(source.read_text())
        entries: dict[str, PublishedHeartbeat] = {}
        for item in payload["feeds"]:
            address = str(item["address"]).lower()
            heartbeat = item["heartbeat_seconds"]
            url = str(item["source_url"])
            if not _ADDRESS.match(address):
                raise ValueError(f"invalid feed address {address!r}")
            if not isinstance(heartbeat, int) or isinstance(heartbeat, bool) or heartbeat <= 0:
                raise ValueError(f"invalid heartbeat for {address}")
            if not url.startswith("https://"):
                raise ValueError(f"a published heartbeat needs an https source URL ({address})")
            if address in entries:
                raise ValueError(f"duplicate feed {address}")
            entries[address] = PublishedHeartbeat(
                address=address,
                description=str(item["description"]),
                provider=str(item["provider"]),
                heartbeat_seconds=heartbeat,
                source_url=url,
            )
        return cls(entries)

    def lookup(self, address: str, description: str | None) -> PublishedHeartbeat | None:
        entry = self._entries.get(address.lower())
        if entry is None or entry.description != description:
            return None
        return entry

    def __len__(self) -> int:
        return len(self._entries)


def default_third_party_registry_path() -> Path:
    return (
        Path(__file__).resolve().parents[4]
        / "data"
        / "catalog"
        / "oracle-feeds.third-party.ethereum.json"
    )


def _address(result: CallResult) -> str | None:
    if not result.success or len(result.data) != 32:
        return None
    try:
        return decode_address(result.data)
    except AbiDecodeError:
        return None


def _uint(result: CallResult) -> int | None:
    if not result.success or len(result.data) != 32:
        return None
    return decode_uint(result.data)


def _string(result: CallResult) -> str | None:
    if not result.success:
        return None
    try:
        return decode_string(result.data)
    except AbiDecodeError:
        return None


def _round(result: CallResult) -> FeedRound | None:
    if not result.success or len(result.data) < 160:
        return None
    return FeedRound(
        round_id=decode_uint(result.data, 0),
        answer=decode_int(result.data, 1),
        updated_at=decode_uint(result.data, 3),
    )
