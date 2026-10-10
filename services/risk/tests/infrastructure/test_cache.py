import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import CachePolicy
from app.infrastructure.cache import (
    DEFAULT_BUSY_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES,
    SQLiteCache,
)
from app.infrastructure.freshness import Freshness


NOW = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
POLICY = CachePolicy(ttl=timedelta(minutes=1), retention=timedelta(minutes=5))


def sqlite_footprint(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for suffix in ("", "-journal", "-wal", "-shm")
        if (candidate := Path(f"{path}{suffix}")).exists()
    )


def test_fresh_hit_returns_payload_and_freshness(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    cache.set("market", "weth-usdc", b"fresh", NOW, POLICY)

    cached = cache.get("market", "weth-usdc", NOW + timedelta(seconds=30))

    assert cached is not None
    assert cached.payload == b"fresh"
    assert cached.fetched_at == NOW
    assert cached.freshness is Freshness.FRESH


def test_stale_but_usable_hit_is_returned_with_stale_freshness(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    cache.set("market", "weth-usdc", b"stale", NOW, POLICY)

    cached = cache.get("market", "weth-usdc", NOW + timedelta(minutes=2))

    assert cached is not None
    assert cached.payload == b"stale"
    assert cached.freshness is Freshness.STALE_USABLE


def test_namespace_is_part_of_cache_identity(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    cache.set("market", "shared", b"market", NOW, POLICY)
    cache.set("oracle", "shared", b"oracle", NOW, POLICY)

    market = cache.get("market", "shared", NOW)
    oracle = cache.get("oracle", "shared", NOW)

    assert market is not None and market.payload == b"market"
    assert oracle is not None and oracle.payload == b"oracle"


def test_older_fetch_cannot_overwrite_newer_cached_value(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    newer = NOW + timedelta(minutes=1)
    cache.set("market", "weth-usdc", b"newer", newer, POLICY)
    cache.set("market", "weth-usdc", b"older", NOW, POLICY)

    cached = cache.get("market", "weth-usdc", newer)

    assert cached is not None
    assert cached.payload == b"newer"
    assert cached.fetched_at == newer


def test_prune_removes_expired_before_more_recent_lru_candidates(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path)
    long_policy = CachePolicy(
        ttl=timedelta(minutes=1), retention=timedelta(hours=1)
    )
    expired_policy = CachePolicy(
        ttl=timedelta(seconds=1), retention=timedelta(seconds=2)
    )
    cache.set("raw", "a-live-oldest", b"l" * 100, NOW, long_policy)
    cache.set("raw", "z-expired-newest", b"e" * 100, NOW, expired_policy)
    assert (
        cache.get("raw", "z-expired-newest", NOW + timedelta(seconds=1))
        is not None
    )

    result = cache.prune(NOW + timedelta(seconds=3), max_bytes=1536)

    assert result.removed_entries == 1
    assert result.remaining_entries == 1
    assert result.remaining_bytes == sqlite_footprint(path) == 1536
    assert cache.get("raw", "a-live-oldest", NOW + timedelta(seconds=3))
    assert cache.get("raw", "z-expired-newest", NOW + timedelta(seconds=3)) is None


def test_set_enforces_actual_file_cap_and_preserves_exact_cap_entry(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path, max_bytes=1536)

    cache.set("raw", "first", b"x" * 100, NOW, POLICY)

    assert sqlite_footprint(path) == 1536
    assert cache.get("raw", "first", NOW) is not None

    cache.set("raw", "second", b"y" * 100, NOW + timedelta(seconds=1), POLICY)

    assert sqlite_footprint(path) <= 1536
    assert cache.get("raw", "first", NOW + timedelta(seconds=1)) is None
    assert cache.get("raw", "second", NOW + timedelta(seconds=1)) is not None


def test_one_kilobyte_cap_evicts_entries_and_reclaims_deleted_pages(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path, max_bytes=1024)

    cache.set("raw", "tiny", b"x", NOW, POLICY)

    assert sqlite_footprint(path) == 1024
    assert cache.get("raw", "tiny", NOW) is None


def test_oversized_single_entry_is_deterministically_not_cached(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path, max_bytes=1536)

    cache.set("raw", "oversized", b"x" * 2000, NOW, POLICY)

    assert sqlite_footprint(path) <= 1536
    assert cache.get("raw", "oversized", NOW) is None


def test_default_cap_is_one_hundred_million_bytes_without_large_fixture(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")

    assert cache.max_bytes == DEFAULT_MAX_BYTES == 100_000_000


def test_cache_rejects_caps_below_sqlite_minimum_footprint(tmp_path):
    with pytest.raises(ValueError, match="at least 1024 bytes"):
        SQLiteCache(tmp_path / "too-small.sqlite", max_bytes=1023)

    cache = SQLiteCache(tmp_path / "cache.sqlite")
    with pytest.raises(ValueError, match="at least 1024 bytes"):
        cache.prune(NOW, max_bytes=1023)


def test_cache_normalizes_dst_fallback_before_duration_arithmetic(tmp_path):
    zurich = ZoneInfo("Europe/Zurich")
    fetched_at = datetime(2026, 10, 25, 2, 45, tzinfo=zurich, fold=0)
    now = datetime(2026, 10, 25, 2, 15, tzinfo=zurich, fold=1)
    policy = CachePolicy(ttl=timedelta(minutes=20), retention=timedelta(hours=1))
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    cache.set("oracle", "eth-usd", b"value", fetched_at, policy)

    cached = cache.get("oracle", "eth-usd", now)

    assert cached is not None
    assert cached.freshness is Freshness.STALE_USABLE
    assert cached.expires_at == datetime(2026, 10, 25, 1, 5, tzinfo=UTC)


def test_cache_rejects_naive_datetimes_at_public_boundaries(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    naive = datetime(2026, 10, 10, 10, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        cache.set("raw", "key", b"value", naive, POLICY)
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.get("raw", "key", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        cache.prune(naive)


@pytest.fixture
def statements(monkeypatch):
    """Every SQL statement the cache runs, in order."""
    import app.infrastructure.cache as cache_module

    recorded: list[str] = []
    configure = cache_module.configure_connection

    def traced(connection):
        configure(connection)
        connection.set_trace_callback(recorded.append)

    monkeypatch.setattr(cache_module, "configure_connection", traced)
    return recorded


def pragma(path: Path, name: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        connection.close()


def use_rollback_journal(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()


def test_new_cache_uses_wal_with_a_busy_timeout(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path, timeout_seconds=2.5)

    assert pragma(path, "journal_mode") == "wal"
    with cache._connect() as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 2500
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    # The last connection checkpoints and removes the WAL, so the cap stays exact.
    assert not Path(f"{path}-wal").exists()
    assert sqlite_footprint(path) == 1024


def test_existing_rollback_journal_cache_is_converted_in_place(tmp_path):
    path = tmp_path / "cache.sqlite"
    SQLiteCache(path).set("raw", "kept", b"value", NOW, POLICY)
    use_rollback_journal(path)
    assert pragma(path, "journal_mode") == "delete"

    cache = SQLiteCache(path)

    assert pragma(path, "journal_mode") == "wal"
    cached = cache.get("raw", "kept", NOW)
    assert cached is not None and cached.payload == b"value"


def test_cache_opens_when_another_connection_blocks_the_wal_switch(tmp_path):
    path = tmp_path / "cache.sqlite"
    SQLiteCache(path).set("raw", "kept", b"value", NOW, POLICY)
    use_rollback_journal(path)
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
    try:
        cache = SQLiteCache(path, timeout_seconds=0.05)
        assert pragma(path, "journal_mode") == "delete"
    finally:
        reader.execute("COMMIT")
        reader.close()

    cached = cache.get("raw", "kept", NOW)
    assert cached is not None and cached.payload == b"value"
    # The switch is retried on the next start.
    SQLiteCache(path)
    assert pragma(path, "journal_mode") == "wal"


def test_set_under_the_cap_runs_no_prune_work(tmp_path, statements):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    statements.clear()

    cache.set("raw", "small", b"x" * 100, NOW, POLICY)

    assert not any(
        statement.lstrip().upper().startswith(("DELETE", "VACUUM", "PRAGMA WAL"))
        for statement in statements
    )


def test_prune_evicts_lru_entries_in_a_batch_and_vacuums_once(tmp_path, statements):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path)
    for index in range(40):
        fetched_at = NOW + timedelta(seconds=index)
        cache.set("raw", f"entry-{index:02}", b"x" * 400, fetched_at, POLICY)
    full = sqlite_footprint(path)
    statements.clear()

    result = cache.prune(NOW + timedelta(seconds=40), max_bytes=full // 2)

    assert sum(statement.strip() == "VACUUM" for statement in statements) == 1
    batches = [
        statement
        for statement in statements
        if "(last_accessed_at, namespace, key) <=" in statement
    ]
    # Deleting rarely frees whole pages before the VACUUM, so the page count
    # lags and a few batches can run, but never one statement per entry.
    assert 1 <= len(batches) < result.removed_entries / 2
    assert result.remaining_bytes == sqlite_footprint(path) <= full // 2
    assert result.removed_entries + result.remaining_entries == 40
    assert result.remaining_entries >= 15
    later = NOW + timedelta(seconds=41)
    survivors = [
        index for index in range(40) if cache.get("raw", f"entry-{index:02}", later)
    ]
    # Least recently used first: exactly the newest entries survive.
    assert survivors == list(range(40 - result.remaining_entries, 40))


def test_prune_terminates_while_a_reader_pins_the_wal(tmp_path):
    path = tmp_path / "cache.sqlite"
    cache = SQLiteCache(path)
    for index in range(20):
        cache.set("raw", f"entry-{index:02}", b"x" * 400, NOW, POLICY)
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
    try:
        started = time.monotonic()
        result = cache.prune(NOW, max_bytes=8192)
        elapsed = time.monotonic() - started
    finally:
        reader.execute("COMMIT")
        reader.close()

    # The checkpoint does not sit out the 10 s busy timeout behind the reader.
    assert elapsed < DEFAULT_BUSY_TIMEOUT_SECONDS / 2
    # Only enough entries for the live pages to fit are evicted, not all of
    # them, and the file meets the cap once the reader lets go.
    assert 0 < result.remaining_entries < 20
    assert cache.prune(NOW, max_bytes=8192).remaining_bytes <= 8192
