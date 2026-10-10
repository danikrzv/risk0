import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.adapters.morpho import MorphoClient
from app.catalog.repository import CatalogRepository
from app.catalog.service import DEFAULT_MAX_CONCURRENCY, CatalogService
from tests.conftest import MORPHO_BASE_URL


CATALOG_PATH = Path(__file__).resolve().parents[4] / "data" / "catalog" / "ethereum.json"
WBTC_USDC = "0x3a85e619751152991742810df6ec69ce473daef99e28a64ab2340d7b7ccfee49"
# Per-request delay standing in for a Morpho round trip.
LATENCY_SECONDS = 0.02


class SlowMorpho:
    """Wraps the recorded Morpho API with latency, an optional gate, and a
    count of requests in flight."""

    def __init__(self, morpho_api, latency: float = LATENCY_SECONDS) -> None:
        self.api = morpho_api
        self.latency = latency
        self.gate = threading.Event()
        self.gate.set()
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            self.gate.wait(5)
            time.sleep(self.latency)
            return self.api.handler(request)
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.fixture
def slow(morpho_api):
    return SlowMorpho(morpho_api)


def build(slow, cache, clock, **options) -> CatalogService:
    client = MorphoClient(
        MORPHO_BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(slow.handler)),
        cache=cache,
        clock=clock,
        sleep=lambda _: None,
    )
    return CatalogService(
        CatalogRepository(CATALOG_PATH), client, clock=clock, **options
    )


def timed(service: CatalogService):
    started = time.perf_counter()
    response = service.list_hydrated()
    return response, time.perf_counter() - started


def test_cold_catalog_hydrates_entries_concurrently(slow, morpho_api, clock, tmp_path):
    from app.infrastructure.cache import SQLiteCache

    serial, serial_seconds = timed(
        build(slow, SQLiteCache(tmp_path / "serial.sqlite3"), clock, max_concurrency=1)
    )
    assert slow.peak == 1
    slow.peak = 0
    concurrent, concurrent_seconds = timed(
        build(slow, SQLiteCache(tmp_path / "concurrent.sqlite3"), clock)
    )

    print(
        f"cold catalog, {LATENCY_SECONDS * 1000:.0f} ms per request: "
        f"serial {serial_seconds:.2f}s, concurrent {concurrent_seconds:.2f}s"
    )
    # Every entry is a few sequential requests, so requests in flight track
    # entries in flight, and the bound holds.
    assert 1 < slow.peak <= DEFAULT_MAX_CONCURRENCY
    assert concurrent_seconds < serial_seconds / 3
    # Same entries, same order, same data.
    assert concurrent.entries == serial.entries


def test_stale_cache_is_served_at_once_while_a_refresh_runs(slow, cache, clock):
    service = build(slow, cache, clock)
    first = service.list_hydrated()
    clock.advance(timedelta(minutes=2))
    slow.gate.clear()  # Morpho now hangs until released

    try:
        stale, elapsed = timed(service)
    finally:
        slow.gate.set()

    assert elapsed < 1
    for entry, previous in zip(stale.entries, first.entries, strict=True):
        assert entry.freshness == "stale_usable"
        assert entry.age_seconds == 120
        assert entry.metrics == previous.metrics
        assert entry.analysis_available == previous.analysis_available
        assert any("refresh from the live source is in progress" in w for w in entry.warnings)

    service.wait_for_refreshes(timeout=10)
    refreshed = service.list_hydrated()

    assert all(entry.freshness == "fresh" for entry in refreshed.entries)
    assert all(entry.age_seconds == 0 for entry in refreshed.entries)


def test_failed_refresh_keeps_stale_data_and_says_the_source_is_down(
    slow, morpho_api, cache, clock
):
    service = build(slow, cache, clock)
    service.list_hydrated()
    clock.advance(timedelta(minutes=5))
    morpho_api.mode = "down"

    service.list_hydrated()
    service.wait_for_refreshes(timeout=10)
    entries = service.list_hydrated().entries

    for entry in entries:
        assert entry.freshness == "stale_usable"
        assert entry.metrics is not None
        assert any("the live source is unavailable" in w for w in entry.warnings)


def test_refresh_that_finds_the_entity_removed_surfaces_it_next_time(
    slow, morpho_api, cache, clock
):
    service = build(slow, cache, clock)
    service.list_hydrated()
    clock.advance(timedelta(minutes=2))
    morpho_api.remove_market(WBTC_USDC)

    served = {entry.entity_id: entry for entry in service.list_hydrated().entries}
    service.wait_for_refreshes(timeout=10)
    after = {entry.entity_id: entry for entry in service.list_hydrated().entries}

    assert served["wbtc-usdc-860"].availability == "available"
    assert after["wbtc-usdc-860"].availability == "not_found"
    assert after["wbtc-usdc-860"].metrics is None
    assert after["weth-usdc-860"].availability == "available"


def test_concurrent_requests_share_one_refresh_per_entry(slow, morpho_api, cache, clock):
    service = build(slow, cache, clock)
    service.list_hydrated()
    clock.advance(timedelta(minutes=2))
    before = len(morpho_api.graphql_requests())
    slow.gate.clear()

    try:
        service.list_hydrated()
        service.list_hydrated()
    finally:
        slow.gate.set()
    service.wait_for_refreshes(timeout=10)

    assert len(morpho_api.graphql_requests()) - before == len(
        service.list_hydrated().entries
    )


def test_max_concurrency_must_be_positive(morpho_client):
    with pytest.raises(ValueError, match="at least 1"):
        CatalogService(CatalogRepository(CATALOG_PATH), morpho_client, max_concurrency=0)
