from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.defillama import FAILURE_BACKOFF, DefiLlamaUnavailableError
from app.adapters.defillama_coins import (
    CLOSED_CHUNK_POLICY,
    MAX_SPAN,
    OPEN_CHUNK_POLICY,
)
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness
from tests.fixtures.defillama_coins.recorded import EURCV, EUROP, FakeCoinsApi


CHUNK_SECONDS = MAX_SPAN * 7200
RECORDED_START = datetime.fromtimestamp(1785600000, UTC)
RECORDED_END = datetime.fromtimestamp(1785600000 + CHUNK_SECONDS - 1, UTC)


@pytest.fixture
def cache(tmp_path):
    return SQLiteCache(tmp_path / "cache.sqlite3")


def test_recorded_chunk_is_parsed_into_decimal_utc_prices(clock):
    api = FakeCoinsApi()

    history = api.client(clock).token_price_history(EURCV, RECORDED_START, RECORDED_END)

    (url,) = api.requests
    assert url.host == "coins.llama.fi"
    assert url.path == f"/chart/ethereum:{EURCV.lower()}"
    assert dict(url.params) == {"start": "1785600000", "span": "500", "period": "2h"}
    assert history.symbol == "EURCV"
    assert history.confidence == Decimal("0.99")
    assert history.period == timedelta(hours=2)
    assert history.chunk_count == 1
    assert history.source == f"defillama-coins https://coins.llama.fi/chart/ethereum:{EURCV.lower()}"
    # 499 recorded points; the first is 419 s before the requested start.
    assert len(history.prices) == 498
    first = history.prices[0]
    assert first.timestamp == datetime.fromtimestamp(1785606784, UTC)
    assert first.price_usd == Decimal("1.153269154765416")
    assert all(isinstance(item.price_usd, Decimal) for item in history.prices)
    assert [item.timestamp for item in history.prices] == sorted(
        item.timestamp for item in history.prices
    )
    assert history.freshness is Freshness.FRESH


def test_a_year_is_read_in_grid_aligned_500_point_chunks(clock):
    now = int(clock().timestamp())
    api = FakeCoinsApi(synthetic_price="1.15", now=now)
    end = clock()
    start = end - timedelta(days=365)

    history = api.client(clock).token_price_history(EURCV, start, end)

    starts = [int(url.params["start"]) for url in api.requests]
    assert all(value % CHUNK_SECONDS == 0 for value in starts)
    assert starts == sorted(starts)
    assert starts[0] <= start.timestamp() < starts[0] + CHUNK_SECONDS
    assert starts[-1] <= end.timestamp() < starts[-1] + CHUNK_SECONDS
    assert len(starts) == history.chunk_count == 10
    assert {url.params["span"] for url in api.requests} == {"500"}
    # Only prices inside [start, end] are kept, each once, oldest first.
    assert history.prices[0].timestamp >= start
    assert history.prices[-1].timestamp <= end
    assert len(history.prices) == len({item.timestamp for item in history.prices})
    assert 4370 <= len(history.prices) <= 4381


def test_unknown_or_not_yet_listed_token_is_an_empty_history_not_an_error(clock):
    api = FakeCoinsApi()

    history = api.client(clock).token_price_history(
        "0x0000000000000000000000000000000000000001", RECORDED_START, RECORDED_END
    )

    assert history.prices == ()
    assert history.symbol is None
    assert history.confidence is None


def test_sparse_recorded_europ_chunk_keeps_its_gaps(clock):
    api = FakeCoinsApi()
    start = datetime.fromtimestamp(1756800000, UTC)

    history = api.client(clock).token_price_history(
        EUROP, start, start + timedelta(seconds=CHUNK_SECONDS - 1)
    )

    assert history.symbol == "EUROP"
    assert len(history.prices) == 43
    gaps = [
        later.timestamp - earlier.timestamp
        for earlier, later in zip(history.prices, history.prices[1:], strict=False)
    ]
    assert max(gaps) > timedelta(days=20)


def test_chunks_are_memoized_and_read_back_from_sqlite(clock, cache):
    api = FakeCoinsApi()
    client = api.client(clock, cache)
    client.token_price_history(EURCV, RECORDED_START, RECORDED_END)
    client.token_price_history(EURCV, RECORDED_START, RECORDED_END)
    assert len(api.requests) == 1

    clock.advance(timedelta(hours=23))
    history = api.client(clock, cache).token_price_history(EURCV, RECORDED_START, RECORDED_END)

    assert len(api.requests) == 1
    assert len(history.prices) == 498
    assert history.prices[0].price_usd == Decimal("1.153269154765416")
    assert history.freshness is Freshness.FRESH


def test_closed_chunks_live_a_month_and_the_open_chunk_a_day(clock, cache):
    now = int(clock().timestamp())
    api = FakeCoinsApi(synthetic_price="1.15", now=now)
    end = clock()
    start = end - timedelta(days=60)
    api.client(clock, cache).token_price_history(EURCV, start, end)
    first_round = len(api.requests)

    clock.advance(OPEN_CHUNK_POLICY.ttl + timedelta(minutes=1))
    api.client(clock, cache).token_price_history(EURCV, start, end)

    refetched = [int(url.params["start"]) for url in api.requests[first_round:]]
    assert refetched == [now // CHUNK_SECONDS * CHUNK_SECONDS]
    assert CLOSED_CHUNK_POLICY.ttl > OPEN_CHUNK_POLICY.ttl


@pytest.mark.parametrize("mode", ["down", "error", "bad-request"])
def test_errors_are_sanitized(clock, mode):
    api = FakeCoinsApi()
    api.mode = mode

    with pytest.raises(DefiLlamaUnavailableError) as raised:
        api.client(clock).token_price_history(EURCV, RECORDED_START, RECORDED_END)

    message = str(raised.value)
    assert message.startswith(f"DeFiLlama coins chart for ethereum:{EURCV.lower()} unavailable")
    for leaked in ("secret", "stack trace", "https://", "span too large"):
        assert leaked not in message
    expected = {"down": "ConnectError", "error": "HTTP 500", "bad-request": "HTTP 400"}[mode]
    assert expected in message
    # 5xx and transport errors are retried once; a 400 is not.
    assert len(api.requests) == (1 if mode == "bad-request" else 2)


def test_outage_serves_stale_chunks_then_fails_after_retention(clock, cache):
    api = FakeCoinsApi()
    api.client(clock, cache).token_price_history(EURCV, RECORDED_START, RECORDED_END)
    api.mode = "down"
    clock.advance(CLOSED_CHUNK_POLICY.ttl + timedelta(days=1))

    stale = api.client(clock, cache).token_price_history(EURCV, RECORDED_START, RECORDED_END)
    assert stale.freshness is Freshness.STALE_USABLE
    assert len(stale.prices) == 498

    clock.advance(CLOSED_CHUNK_POLICY.retention)
    with pytest.raises(DefiLlamaUnavailableError):
        api.client(clock, cache).token_price_history(EURCV, RECORDED_START, RECORDED_END)


def test_failed_fetch_is_not_retried_until_the_backoff_passes(clock):
    api = FakeCoinsApi()
    client = api.client(clock)
    api.mode = "down"
    with pytest.raises(DefiLlamaUnavailableError):
        client.token_price_history(EURCV, RECORDED_START, RECORDED_END)
    attempts = len(api.requests)

    api.mode = "ok"
    with pytest.raises(DefiLlamaUnavailableError, match="retry deferred"):
        client.token_price_history(EURCV, RECORDED_START, RECORDED_END)
    assert len(api.requests) == attempts

    clock.advance(FAILURE_BACKOFF)
    assert client.token_price_history(EURCV, RECORDED_START, RECORDED_END).prices
    assert len(api.requests) == attempts + 1


def test_malformed_payloads_and_rows(clock):
    api = FakeCoinsApi()
    key = (f"ethereum:{EURCV.lower()}", 1785600000)
    api.bodies[key] = b'{"prices": []}'
    with pytest.raises(DefiLlamaUnavailableError, match="no coins object"):
        api.client(clock).token_price_history(EURCV, RECORDED_START, RECORDED_END)

    api.bodies[key] = (
        b'{"coins": {"ethereum:' + EURCV.lower().encode() + b'": {"symbol": "EURCV", '
        b'"prices": [{"timestamp": 1785610000, "price": 1.15}, {"timestamp": 1785620000}, '
        b'{"timestamp": 1785630000, "price": -1}, "text", {"timestamp": true, "price": 1}]}}}'
    )
    (price,) = api.client(clock).token_price_history(EURCV, RECORDED_START, RECORDED_END).prices
    assert price.price_usd == Decimal("1.15")


def test_invalid_arguments_are_rejected(clock):
    client = FakeCoinsApi().client(clock)
    with pytest.raises(ValueError, match="period"):
        client.token_price_history(EURCV, RECORDED_START, RECORDED_END, period=timedelta(hours=3))
    with pytest.raises(ValueError, match="precede"):
        client.token_price_history(EURCV, RECORDED_END, RECORDED_START)
