from datetime import UTC, datetime, timedelta

from app.config import CachePolicy
from app.infrastructure.cache import SQLiteCache
from app.infrastructure.freshness import Freshness


NOW = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
POLICY = CachePolicy(ttl=timedelta(minutes=1), retention=timedelta(minutes=5))


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


def test_expired_entry_is_not_returned_as_fresh_critical_data(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    cache.set("oracle", "eth-usd", b"critical", NOW, POLICY)

    cached = cache.get("oracle", "eth-usd", NOW + POLICY.retention)

    assert cached is None


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


def test_prune_removes_expired_then_lru_entries_to_meet_one_kilobyte_cap(
    tmp_path,
):
    cache = SQLiteCache(tmp_path / "cache.sqlite")
    long_policy = CachePolicy(
        ttl=timedelta(minutes=1), retention=timedelta(hours=1)
    )
    expired_policy = CachePolicy(
        ttl=timedelta(seconds=1), retention=timedelta(seconds=2)
    )
    cache.set("raw", "expired", b"x" * 600, NOW, expired_policy)
    cache.set("raw", "least-recent", b"y" * 600, NOW, long_policy)
    cache.set("raw", "recent", b"z" * 600, NOW, long_policy)
    assert cache.get("raw", "recent", NOW + timedelta(minutes=2)) is not None

    result = cache.prune(NOW + timedelta(minutes=3), max_bytes=1024)

    assert result.removed_entries == 2
    assert result.removed_bytes == 1200
    assert result.remaining_entries == 1
    assert result.remaining_bytes == 600
    assert cache.get("raw", "expired", NOW + timedelta(minutes=3)) is None
    assert cache.get("raw", "least-recent", NOW + timedelta(minutes=3)) is None
    assert cache.get("raw", "recent", NOW + timedelta(minutes=3)) is not None
