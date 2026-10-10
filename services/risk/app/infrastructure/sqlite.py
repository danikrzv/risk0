import sqlite3


# Most WAL files stay far smaller; this only bounds what a checkpoint leaves behind.
JOURNAL_SIZE_LIMIT_BYTES = 4_000_000


def is_busy_error(error: BaseException) -> bool:
    """True for SQLite lock contention (SQLITE_BUSY / SQLITE_LOCKED), not real faults."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return "locked" in message or "busy" in message


def configure_connection(connection: sqlite3.Connection) -> None:
    """Per-connection settings; the busy timeout is passed to `sqlite3.connect`."""
    # NORMAL is durable across application crashes in WAL mode; only an OS
    # crash or power loss can roll back the most recent commits.
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT_BYTES}")


def enable_wal(connection: sqlite3.Connection) -> str:
    """Switch the file to write-ahead logging so readers never wait on a writer.

    The mode is persistent, so this is a no-op on an already converted file. The
    switch needs a moment without other connections; if another process keeps the
    file busy past the timeout, the file keeps its current mode until the next start.
    """
    try:
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.OperationalError as error:
        if not is_busy_error(error):
            raise
        row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower()
