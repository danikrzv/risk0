import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import CachePolicy
from app.infrastructure.freshness import Freshness


DEFAULT_MAX_BYTES = 100_000_000
MINIMUM_DATABASE_BYTES = 1024


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
        _validate_max_bytes(max_bytes)
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
        now_utc = _utc(now)
        now_value = _timestamp(now_utc)
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
            if now_utc >= retention_until:
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
            now=now_utc,
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
        fetched_utc = _utc(fetched_at)
        fetched_value = _timestamp(fetched_utc)
        expires_value = _timestamp(fetched_utc + policy.ttl)
        retention_value = _timestamp(fetched_utc + policy.retention)
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
        self.prune(fetched_utc, self.max_bytes)

    def prune(
        self,
        now: datetime,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> PruneResult:
        _validate_max_bytes(max_bytes)

        now_value = _timestamp(now)
        initial_bytes = self._footprint()
        removed_entries = 0
        with self._connect() as connection:
            expired = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                FROM cache_entries
                WHERE retention_until <= ?
                """,
                (now_value,),
            ).fetchone()
            removed_entries += expired[0]
            connection.execute(
                "DELETE FROM cache_entries WHERE retention_until <= ?",
                (now_value,),
            )
            connection.commit()
            if expired[0]:
                connection.execute("VACUUM")

            remaining_entries = connection.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]
            while self._footprint() > max_bytes and remaining_entries:
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
                connection.commit()
                connection.execute("VACUUM")
                removed_entries += 1
                remaining_entries -= 1

        remaining_bytes = self._footprint()

        return PruneResult(
            removed_entries=removed_entries,
            removed_bytes=max(0, initial_bytes - remaining_bytes),
            remaining_entries=remaining_entries,
            remaining_bytes=remaining_bytes,
        )

    def _initialize(self) -> None:
        new_database = not self.path.exists()
        if new_database:
            connection = sqlite3.connect(self.path)
            try:
                connection.execute("PRAGMA page_size = 512")
                connection.execute("VACUUM")
            finally:
                connection.close()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries("
                "namespace TEXT NOT NULL,key TEXT NOT NULL,payload BLOB NOT NULL,"
                "fetched_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
                "retention_until TEXT NOT NULL,byte_size INTEGER NOT NULL,"
                "last_accessed_at TEXT NOT NULL,PRIMARY KEY(namespace,key)"
                ") WITHOUT ROWID"
            )

    def _footprint(self) -> int:
        return sum(
            candidate.stat().st_size
            for suffix in ("", "-journal", "-wal", "-shm")
            if (candidate := Path(f"{self.path}{suffix}")).exists()
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
    return _utc(value).isoformat(timespec="microseconds")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_max_bytes(max_bytes: int) -> None:
    if max_bytes < MINIMUM_DATABASE_BYTES:
        raise ValueError("cache size cap must be at least 1024 bytes")
