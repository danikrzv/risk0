import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import CachePolicy
from app.infrastructure.freshness import Freshness


DEFAULT_MAX_BYTES = 100_000_000


@dataclass(frozen=True, slots=True)
class CachedValue:
    payload: bytes
    fetched_at: datetime
    expires_at: datetime
    retention_until: datetime
    freshness: Freshness


@dataclass(frozen=True, slots=True)
class PruneResult:
    removed_entries: int
    removed_bytes: int
    remaining_entries: int
    remaining_bytes: int


class SQLiteCache:
    def __init__(
        self,
        path: str | Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("cache size cap cannot be negative")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(
        self,
        namespace: str,
        key: str,
        now: datetime,
    ) -> CachedValue | None:
        now_value = _timestamp(now)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload, fetched_at, expires_at, retention_until
                FROM cache_entries
                WHERE namespace = ? AND key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None:
                return None

            retention_until = datetime.fromisoformat(row[3])
            if now >= retention_until:
                connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                    (namespace, key),
                )
                return None

            connection.execute(
                """
                UPDATE cache_entries
                SET last_accessed_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (now_value, namespace, key),
            )

        fetched_at = datetime.fromisoformat(row[1])
        expires_at = datetime.fromisoformat(row[2])
        freshness = Freshness.from_times(
            fetched_at=fetched_at,
            now=now,
            ttl=expires_at - fetched_at,
            maximum_age=retention_until - fetched_at,
        )
        return CachedValue(
            payload=bytes(row[0]),
            fetched_at=fetched_at,
            expires_at=expires_at,
            retention_until=retention_until,
            freshness=freshness,
        )

    def set(
        self,
        namespace: str,
        key: str,
        payload: bytes,
        fetched_at: datetime,
        policy: CachePolicy,
    ) -> None:
        payload_bytes = bytes(payload)
        fetched_value = _timestamp(fetched_at)
        expires_value = _timestamp(fetched_at + policy.ttl)
        retention_value = _timestamp(fetched_at + policy.retention)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries (
                    namespace,
                    key,
                    payload,
                    fetched_at,
                    expires_at,
                    retention_until,
                    byte_size,
                    last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    payload = excluded.payload,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at,
                    retention_until = excluded.retention_until,
                    byte_size = excluded.byte_size,
                    last_accessed_at = excluded.last_accessed_at
                WHERE excluded.fetched_at >= cache_entries.fetched_at
                """,
                (
                    namespace,
                    key,
                    sqlite3.Binary(payload_bytes),
                    fetched_value,
                    expires_value,
                    retention_value,
                    len(payload_bytes),
                    fetched_value,
                ),
            )
        self.prune(fetched_at, self.max_bytes)

    def prune(
        self,
        now: datetime,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> PruneResult:
        if max_bytes < 0:
            raise ValueError("cache size cap cannot be negative")

        removed_entries = 0
        removed_bytes = 0
        with self._connect() as connection:
            expired = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                FROM cache_entries
                WHERE retention_until <= ?
                """,
                (_timestamp(now),),
            ).fetchone()
            removed_entries += expired[0]
            removed_bytes += expired[1]
            connection.execute(
                "DELETE FROM cache_entries WHERE retention_until <= ?",
                (_timestamp(now),),
            )

            remaining_entries, remaining_bytes = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                FROM cache_entries
                """
            ).fetchone()
            while remaining_bytes > max_bytes:
                oldest = connection.execute(
                    """
                    SELECT namespace, key, byte_size
                    FROM cache_entries
                    ORDER BY last_accessed_at, namespace, key
                    LIMIT 1
                    """
                ).fetchone()
                connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
                    (oldest[0], oldest[1]),
                )
                removed_entries += 1
                removed_bytes += oldest[2]
                remaining_entries -= 1
                remaining_bytes -= oldest[2]

        return PruneResult(
            removed_entries=removed_entries,
            removed_bytes=removed_bytes,
            remaining_entries=remaining_entries,
            remaining_bytes=remaining_bytes,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    retention_until TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    last_accessed_at TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS cache_entries_retention
                ON cache_entries(retention_until)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS cache_entries_last_accessed
                ON cache_entries(last_accessed_at, namespace, key)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
