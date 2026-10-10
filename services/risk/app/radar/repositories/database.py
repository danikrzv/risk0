import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


MIGRATIONS_DIRECTORY = Path(__file__).parent / "migrations"
_DECIMAL_TAG = "$decimal"


def default_radar_path(cache_path: str | Path) -> Path:
    """Sibling of the cache file. Sharing one file would make the cache's
    whole-file size cap evict (and VACUUM) against the radar tables too."""
    cache_path = Path(cache_path)
    return cache_path.with_name(f"{cache_path.stem}-radar{cache_path.suffix}")


class RadarDatabase:
    """SQLite home of the radar tables; may share a file with `SQLiteCache`."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 30) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout_seconds = timeout_seconds
        self._local = threading.local()
        self._revision = 0
        self._revision_lock = threading.Lock()

    @property
    def revision(self) -> int:
        """Bumped after every committed transaction that changed a row.

        Lets read models (e.g. the graph endpoint) cache safely within this
        process. Writes made by another process are not observed.
        """
        return self._revision

    def _bump_revision(self) -> None:
        with self._revision_lock:
            self._revision += 1

    def migrate(self) -> None:
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS radar_schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM radar_schema_migrations"
                )
            }
            for migration in sorted(MIGRATIONS_DIRECTORY.glob("*.sql")):
                if migration.stem in applied:
                    continue
                # Every statement is IF NOT EXISTS and the version insert is
                # OR IGNORE, so a concurrent or interrupted migration re-runs safely.
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration.read_text()
                    + "\nINSERT OR IGNORE INTO radar_schema_migrations "
                    f"(version, applied_at) VALUES ('{migration.stem}', "
                    f"'{encode_timestamp(datetime.now(UTC))}');\nCOMMIT;"
                )
        self._bump_revision()

    def applied_migrations(self) -> tuple[str, ...]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT version FROM radar_schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(row[0] for row in rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside one transaction; nested calls join the outer one."""
        current = getattr(self._local, "connection", None)
        if current is not None:
            yield current
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                if connection.total_changes:
                    self._bump_revision()
            finally:
                self._local.connection = None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path, timeout=self._timeout_seconds, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()


def encode_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("radar timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def decode_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def optional_timestamp(value: str | None) -> datetime | None:
    return None if value is None else decode_timestamp(value)


def encode_decimal(value: Decimal | None) -> str | None:
    # str() round-trips exactly, including precision beyond 28 digits.
    return None if value is None else str(value)


def decode_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def encode_json(value: Any) -> str:
    return json.dumps(_tag(value), sort_keys=True, separators=(",", ":"))


def decode_json(text: str) -> Any:
    return _untag(json.loads(text))


def encode_cursor(timestamp: str, key: str) -> str:
    return f"{timestamp}|{key}"


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Split a keyset cursor into its normalized UTC timestamp and tie-break key."""
    timestamp, separator, key = cursor.partition("|")
    try:
        if not separator or not key:
            raise ValueError
        normalized = encode_timestamp(datetime.fromisoformat(timestamp))
    except ValueError:
        raise ValueError("invalid pagination cursor") from None
    return normalized, key


def encode_strings(values: Sequence[str]) -> str:
    return json.dumps(list(values))


def decode_strings(text: str) -> tuple[str, ...]:
    return tuple(json.loads(text))


def _tag(value: Any) -> Any:
    # Decimals are tagged so they never collapse into strings (or floats) on reload.
    if isinstance(value, Decimal):
        return {_DECIMAL_TAG: str(value)}
    if isinstance(value, float):
        raise TypeError("binary floats cannot be persisted as radar values")
    if isinstance(value, Mapping):
        return {str(key): _tag(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_tag(item) for item in value]
    return value


def _untag(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {_DECIMAL_TAG}:
            return Decimal(value[_DECIMAL_TAG])
        return {key: _untag(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_untag(item) for item in value]
    return value
