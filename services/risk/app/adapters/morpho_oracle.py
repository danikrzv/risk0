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

A decoded feed is classified by what it is, never by its name alone:
- `chainlink`: answers `phaseId()` and a `latestRoundData()` with a non-zero
  round id and update time, so it has a heartbeat and a round history.
- `exchange_rate_adapter`: one of the documented on-chain rate adapters below.
  They compute the answer when read (round id and update time are zero), so
  they cannot be stale; their risk is the rate source itself.
- `unknown`: anything else. An oracle with an unknown feed is not reported as
  healthy: the health provider treats it as unobservable.

Selectors were computed offline (keccak256) and verified with eth_call on the
WETH/USDC 86% market oracle 0x0f948cba8231db7898ef36a4212581ad7b1b4580 (V2:
BASE_FEED_1 = an ETH / USD proxy, SCALE_FACTOR = 1e16, price() = 2508.34e24 on
2026-10-11) and the WBTC/USDC 86% oracle 0xdddd770badd886df3864029e4b377b5f6a2b6b83
(V1: VAULT = zero, BASE_VAULT reverts).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.adapters.ethereum import (
    DECIMALS_SELECTOR,
    DESCRIPTION_SELECTOR,
    LATEST_ROUND_DATA_SELECTOR,
    PHASE_ID_SELECTOR,
    FeedRound,
)
from app.adapters.multicall import (
    AbiDecodeError,
    Call,
    CallResult,
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


class FeedKind(StrEnum):
    CHAINLINK = "chainlink"
    EXCHANGE_RATE_ADAPTER = "exchange_rate_adapter"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OracleFeed:
    role: str
    address: str
    kind: FeedKind
    description: str | None
    decimals: int | None


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


class Aggregator(Protocol):
    """The slice of `EthereumClient` this module uses."""

    def aggregate(self, calls: Sequence[Call]) -> tuple[CallResult, ...]: ...


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
        """`price()` and every feed's `latestRoundData()` in one multicall."""
        calls = [Call(oracle.address, PRICE_SELECTOR)]
        calls.extend(Call(feed.address, LATEST_ROUND_DATA_SELECTOR) for feed in oracle.feeds)
        results = self._client.aggregate(calls)
        return OracleState(
            price=_uint(results[0]),
            latest=tuple(_round(result) for result in results[1:]),
        )

    def _classify(self, used: Sequence[tuple[str, str]]) -> tuple[OracleFeed, ...]:
        if not used:
            return ()
        calls: list[Call] = []
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
        feeds: list[OracleFeed] = []
        for index, (role, feed) in enumerate(used):
            description_result, decimals_result, phase_result, latest_result = results[
                4 * index : 4 * index + 4
            ]
            description = _string(description_result)
            decimals = _uint(decimals_result)
            phase = _uint(phase_result)
            latest = _round(latest_result)
            if decimals is not None and decimals > 77:
                decimals = None
            if (
                phase is not None
                and phase > 0
                and decimals is not None
                and latest is not None
                and latest.round_id > 0
                and latest.updated_at > 0
            ):
                kind = FeedKind.CHAINLINK
            elif (
                KNOWN_RATE_ADAPTERS.get(feed) is not None
                and KNOWN_RATE_ADAPTERS[feed] == description
                and decimals is not None
                and latest is not None
            ):
                kind = FeedKind.EXCHANGE_RATE_ADAPTER
            else:
                kind = FeedKind.UNKNOWN
            feeds.append(OracleFeed(role, feed, kind, description, decimals))
        return tuple(feeds)


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
