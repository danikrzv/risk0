import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import CachePolicy
from app.infrastructure.freshness import Freshness
from app.infrastructure.sqlite import configure_connection, enable_wal


DEFAULT_MAX_BYTES = 100_000_000
MINIMUM_DATABASE_BYTES = 1024
# How long a connection waits on another writer before "database is locked".
DEFAULT_BUSY_TIMEOUT_SECONDS = 10


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
        *,
        timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        _validate_max_bytes(max_bytes)
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._timeout_seconds = timeout_seconds
        self._prune_lock = threading.Lock()
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
        # A stat() suffices while under the cap: a WAL only overstates the
        # size, and _prune() checkpoints before it measures again.
        if self._footprint() > self.max_bytes and self._prune_lock.acquire(
            blocking=False
        ):
            # Otherwise another thread is already pruning; the next write re-checks.
            try:
                self._prune(_timestamp(fetched_utc), self.max_bytes)
            finally:
                self._prune_lock.release()

    def prune(
        self,
        now: datetime,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> PruneResult:
        _validate_max_bytes(max_bytes)
        now_value = _timestamp(now)
        with self._prune_lock:
            return self._prune(now_value, max_bytes)

    def _prune(self, now_value: str, max_bytes: int) -> PruneResult:
        initial_bytes = self._footprint()
        with self._connect() as connection:
            removed_entries = connection.execute(
                "DELETE FROM cache_entries WHERE retention_until <= ?",
                (now_value,),
            ).rowcount
            connection.commit()

            removed_now = removed_entries
            while True:
                if _live_bytes(connection) > max_bytes:
                    evicted = _evict_lru(connection, max_bytes)
                    removed_entries += evicted
                    removed_now += evicted
                if _free_pages(connection) and (
                    removed_now or self._footprint() > max_bytes
                ):
                    # Space is reclaimed once per batch, never per evicted row.
                    connection.execute("VACUUM")
                removed_now = 0
                _checkpoint(connection)
                if self._footprint() <= max_bytes:
                    break
                if _live_bytes(connection) <= max_bytes or not _has_entries(
                    connection
                ):
                    # Another connection pins the WAL (or nothing is left to
                    # evict); dropping more entries would not shrink the file.
                    break

            remaining_entries = connection.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]

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
            connection = sqlite3.connect(self.path, timeout=self._timeout_seconds)
            try:
                # Must precede WAL mode, which fixes the page size.
                connection.execute("PRAGMA page_size = 512")
                connection.execute("VACUUM")
            finally:
                connection.close()
        with self._connect() as connection:
            # Persistent; converts existing rollback-journal files in place.
            enable_wal(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries("
                "namespace TEXT NOT NULL,key TEXT NOT NULL,payload BLOB NOT NULL,"
                "fetched_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
                "retention_until TEXT NOT NULL,byte_size INTEGER NOT NULL,"
                "last_accessed_at TEXT NOT NULL,PRIMARY KEY(namespace,key)"
                ") WITHOUT ROWID"
            )

    def _footprint(self) -> int:
        # The -shm wal-index is shared memory that SQLite deletes with the last
        # connection; it holds no data, so a connection open to measure would
        # otherwise count its own index against the cap.
        return sum(
            candidate.stat().st_size
            for suffix in ("", "-journal", "-wal")
            if (candidate := Path(f"{self.path}{suffix}")).exists()
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self._timeout_seconds)
        connection.row_factory = sqlite3.Row
        try:
            configure_connection(connection)
            with connection:
                yield connection
        finally:
            connection.close()


def _evict_lru(connection: sqlite3.Connection, max_bytes: int) -> int:
    """Delete least recently used entries in batches until the live pages fit.

    Each candidate is credited with its record plus a whole page, an upper bound
    on what deleting it frees, so a batch never evicts past what the excess
    needs; a batch that falls short is followed by the next one.
    """
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    candidates = connection.execute(
        """
        SELECT last_accessed_at, namespace, key,
            byte_size + length(namespace) + length(key) + length(fetched_at)
                + length(expires_at) + length(retention_until)
                + length(last_accessed_at) + ?
        FROM cache_entries
        ORDER BY last_accessed_at, namespace, key
        """,
        (page_size,),
    ).fetchall()
    evicted = 0
    position = 0
    excess = _live_bytes(connection) - max_bytes
    while excess > 0 and position < len(candidates):
        credited = 0
        while position < len(candidates) and credited < excess:
            credited += candidates[position][3]
            position += 1
        cutoff = candidates[position - 1]
        # The LRU order is total, so one range delete drops the whole batch.
        evicted += connection.execute(
            """
            DELETE FROM cache_entries
            WHERE (last_accessed_at, namespace, key) <= (?, ?, ?)
            """,
            (cutoff[0], cutoff[1], cutoff[2]),
        ).rowcount
        connection.commit()
        excess = _live_bytes(connection) - max_bytes
    return evicted


def _live_bytes(connection: sqlite3.Connection) -> int:
    """The size the database shrinks to once its free pages are vacuumed."""
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    pages = connection.execute("PRAGMA page_count").fetchone()[0]
    return (pages - _free_pages(connection)) * page_size


def _free_pages(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA freelist_count").fetchone()[0]


def _has_entries(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute("SELECT 1 FROM cache_entries LIMIT 1").fetchone()
        is not None
    )


def _checkpoint(connection: sqlite3.Connection) -> None:
    # Fold the WAL into the database file and truncate it so the file sizes
    # reflect the data; a no-op outside WAL mode. A TRUNCATE checkpoint would
    # wait out the busy timeout for readers of older snapshots, so it runs
    # without one and settles for a partial checkpoint instead.
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    connection.execute("PRAGMA busy_timeout = 0")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.execute(f"PRAGMA busy_timeout = {int(timeout)}")


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _validate_max_bytes(max_bytes: int) -> None:
    if max_bytes < MINIMUM_DATABASE_BYTES:
        raise ValueError("cache size cap must be at least 1024 bytes")
