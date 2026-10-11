"""DeFiLlama coins API adapter: historical USD prices of an Ethereum token.

Reads the keyless `https://coins.llama.fi/chart/ethereum:<address>` endpoint,
which returns `{"coins": {"ethereum:<address>": {"symbol", "confidence",
"decimals", "prices": [{"timestamp", "price"}, ...]}}}`: `span` points `period`
apart from `start`. DeFiLlama prices are aggregated market prices (DEX pools and
other venues), sampled at each point, not an interval low.

Endpoint limits measured on 2026-10-11 (EURCV, EUROP and USDC over 365 days):

- `span` above 500 is rejected with HTTP 400 for every period (1h, 2h, 4h).
- `period=1h` returns about 78% of the hours for EURCV and 21% for EUROP, with
  gaps of up to a week; `period=2h` returns 4376 of 4380 points for EURCV (no gap
  over 4 hours) and 4321 for EUROP (one 3-day gap), in 9 requests of 500 points.

So the default resolution is 2 hours, the finest that covers a year almost
completely, read in 500-point chunks (41 days 16 hours each). Chunks sit on a
fixed UTC grid (multiples of the chunk length since the Unix epoch), so a
chunk's cache key does not move with the request time: a chunk that ended more
than a day before it was fetched is history and is cached for 30 days; the
chunk still being written is cached for a day (spec § 9: daily refresh). Either
stays usable as stale data while DeFiLlama is unreachable.

Transport, retries, the failure backoff and error sanitization are those of
`DefiLlamaClient`: messages name the token and an HTTP status or exception type,
never a response body.
"""

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.adapters.defillama import (
    FAILURE_BACKOFF,
    DefiLlamaClient,
    DefiLlamaUnavailableError,
    _ProviderError,
)
from app.config import CachePolicy
from app.infrastructure.freshness import Freshness


COINS_BASE_URL = "https://coins.llama.fi"
COINS_NAMESPACE = "defillama-coins:v1"
CHAIN_PREFIX = "ethereum"
MAX_SPAN = 500
DEFAULT_PERIOD = timedelta(hours=2)
SUPPORTED_PERIODS = {
    timedelta(hours=1): "1h",
    timedelta(hours=2): "2h",
    timedelta(hours=4): "4h",
    timedelta(days=1): "1d",
}
OPEN_CHUNK_POLICY = CachePolicy(ttl=timedelta(days=1), retention=timedelta(days=7))
CLOSED_CHUNK_POLICY = CachePolicy(ttl=timedelta(days=30), retention=timedelta(days=400))
# A chunk that ended this long before it was fetched is treated as final.
SETTLEMENT_DELAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class TokenPrice:
    timestamp: datetime
    price_usd: Decimal


@dataclass(frozen=True, slots=True)
class TokenPriceHistory:
    """USD prices of one token in [start, end], oldest first."""

    address: str
    symbol: str | None
    # Lowest DeFiLlama price confidence (0-1) over the chunks read; None without data.
    confidence: Decimal | None
    period: timedelta
    start: datetime
    end: datetime
    prices: tuple[TokenPrice, ...]
    source: str
    # Oldest fetch time among the chunks used, and the worst chunk freshness.
    fetched_at: datetime
    freshness: Freshness
    chunk_count: int


@dataclass(frozen=True, slots=True)
class _Chunk:
    symbol: str | None
    confidence: Decimal | None
    prices: tuple[TokenPrice, ...]
    fetched_at: datetime
    freshness: Freshness


class DefiLlamaCoinsClient(DefiLlamaClient):
    def __init__(self, base_url: str = COINS_BASE_URL, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)
        self._chunk_memo: dict[str, tuple[datetime, _Chunk]] = {}
        self._chunk_lock = threading.Lock()

    def token_price_history(
        self,
        address: str,
        start: datetime,
        end: datetime,
        *,
        period: timedelta = DEFAULT_PERIOD,
    ) -> TokenPriceHistory:
        """Every DeFiLlama price of the Ethereum token `address` in [start, end].

        Raises `DefiLlamaUnavailableError` when a chunk can be neither fetched
        nor served from a usable cache entry.
        """
        if period not in SUPPORTED_PERIODS:
            raise ValueError(f"unsupported DeFiLlama chart period {period}")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end < start:
            raise ValueError("end must not precede start")
        coin = f"{CHAIN_PREFIX}:{address.lower()}"
        chunk_seconds = int((period * MAX_SPAN).total_seconds())
        lower = int(start.astimezone(UTC).timestamp())
        upper = int(end.astimezone(UTC).timestamp())
        chunks = [
            self._chunk(coin, index * chunk_seconds, period, chunk_seconds)
            for index in range(lower // chunk_seconds, upper // chunk_seconds + 1)
        ]
        by_timestamp: dict[datetime, TokenPrice] = {}
        for chunk in chunks:
            for price in chunk.prices:
                if lower <= int(price.timestamp.timestamp()) <= upper:
                    by_timestamp[price.timestamp] = price
        confidences = [item.confidence for item in chunks if item.confidence is not None]
        symbol = next((item.symbol for item in chunks if item.symbol), None)
        return TokenPriceHistory(
            address=address.lower(),
            symbol=symbol,
            confidence=min(confidences) if confidences else None,
            period=period,
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            prices=tuple(by_timestamp[key] for key in sorted(by_timestamp)),
            source=f"defillama-coins {self._base_url}/chart/{coin}",
            fetched_at=min(item.fetched_at for item in chunks),
            freshness=max(
                (item.freshness for item in chunks), key=_FRESHNESS_ORDER.index
            ),
            chunk_count=len(chunks),
        )

    def _chunk(self, coin: str, chunk_start: int, period: timedelta, chunk_seconds: int) -> _Chunk:
        label = SUPPORTED_PERIODS[period]
        path = f"/chart/{coin}?start={chunk_start}&span={MAX_SPAN}&period={label}"
        now = self._clock()
        with self._chunk_lock:
            memo = self._chunk_memo.get(path)
        if memo is not None and now < memo[0]:
            return memo[1]

        cached = self._cache.get(COINS_NAMESPACE, path, now) if self._cache is not None else None
        if cached is not None and cached.freshness is Freshness.FRESH:
            chunk = _decode(cached.payload, cached.fetched_at, cached.freshness)
            self._remember_chunk(path, chunk, cached.expires_at)
            return chunk

        with self._lock:
            failed_until = self._failed_until.get(coin)
        try:
            if failed_until is not None and now < failed_until:
                raise _ProviderError("recent failure; retry deferred")
            fetched_at = self._clock()
            symbol, confidence, prices = _parse_chart(self._request(path), coin)
        except (_ProviderError, ValueError, TypeError, KeyError, ArithmeticError) as error:
            if failed_until is None or now >= failed_until:
                with self._lock:
                    self._failed_until[coin] = now + FAILURE_BACKOFF
            if cached is not None and cached.freshness is not Freshness.EXPIRED:
                stale = _decode(cached.payload, cached.fetched_at, cached.freshness)
                self._remember_chunk(path, stale, now + FAILURE_BACKOFF)
                return stale
            reason = str(error) if isinstance(error, _ProviderError) else type(error).__name__
            raise DefiLlamaUnavailableError(
                f"DeFiLlama coins chart for {coin} unavailable: {reason}"
            ) from None

        with self._lock:
            self._failed_until.pop(coin, None)
        chunk = _Chunk(symbol, confidence, prices, fetched_at, Freshness.FRESH)
        chunk_end = datetime.fromtimestamp(chunk_start + chunk_seconds, UTC)
        policy = (
            CLOSED_CHUNK_POLICY
            if chunk_end + SETTLEMENT_DELAY <= fetched_at
            else OPEN_CHUNK_POLICY
        )
        if self._cache is not None:
            self._cache.set(COINS_NAMESPACE, path, _encode(chunk), fetched_at, policy)
        self._remember_chunk(path, chunk, fetched_at + policy.ttl)
        return chunk

    def _remember_chunk(self, path: str, chunk: _Chunk, expires_at: datetime) -> None:
        with self._chunk_lock:
            self._chunk_memo[path] = (expires_at, chunk)


_FRESHNESS_ORDER = [Freshness.FRESH, Freshness.STALE_USABLE, Freshness.EXPIRED]


def _parse_chart(
    payload: Any, coin: str
) -> tuple[str | None, Decimal | None, tuple[TokenPrice, ...]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("coins"), dict):
        raise _ProviderError("chart payload has no coins object")
    # DeFiLlama keys the coin as requested; match case-insensitively to be safe.
    entry = next(
        (value for key, value in payload["coins"].items() if key.lower() == coin),
        None,
    )
    if entry is None:
        # No price for this token in the chunk (not listed yet, or unknown).
        return None, None, ()
    if not isinstance(entry, dict) or not isinstance(entry.get("prices"), list):
        raise _ProviderError("chart entry has no price list")
    prices: dict[int, Decimal] = {}
    for item in entry["prices"]:
        if not isinstance(item, dict):
            continue
        timestamp, price = _number(item.get("timestamp")), _number(item.get("price"))
        if timestamp is None or price is None or price <= 0:
            continue
        prices[int(timestamp)] = price
    symbol = entry.get("symbol")
    return (
        symbol.strip() if isinstance(symbol, str) and symbol.strip() else None,
        _number(entry.get("confidence")),
        tuple(
            TokenPrice(datetime.fromtimestamp(key, UTC), prices[key]) for key in sorted(prices)
        ),
    )


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return None


def _encode(chunk: _Chunk) -> bytes:
    return json.dumps(
        {
            "symbol": chunk.symbol,
            "confidence": None if chunk.confidence is None else str(chunk.confidence),
            "prices": [
                [int(item.timestamp.timestamp()), str(item.price_usd)] for item in chunk.prices
            ],
        },
        separators=(",", ":"),
    ).encode()


def _decode(payload: bytes, fetched_at: datetime, freshness: Freshness) -> _Chunk:
    value = json.loads(payload)
    return _Chunk(
        symbol=value["symbol"],
        confidence=None if value["confidence"] is None else Decimal(value["confidence"]),
        prices=tuple(
            TokenPrice(datetime.fromtimestamp(stamp, UTC), Decimal(price))
            for stamp, price in value["prices"]
        ),
        fetched_at=fetched_at,
        freshness=freshness,
    )
