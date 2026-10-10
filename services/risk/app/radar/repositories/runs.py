import sqlite3
from typing import Literal

from app.radar.contracts import SchedulerRun
from app.radar.repositories.database import (
    RadarDatabase,
    decode_timestamp,
    encode_timestamp,
    optional_timestamp,
)


class SchedulerRunRepository:
    """Scheduler cycle records, including skipped overlapping cycles."""

    def __init__(self, database: RadarDatabase) -> None:
        self._db = database

    def record(self, run: SchedulerRun) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_runs (
                    run_id, loop, status, started_at, finished_at, processed_count,
                    error_count, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = excluded.status,
                    finished_at = excluded.finished_at,
                    processed_count = excluded.processed_count,
                    error_count = excluded.error_count,
                    message = excluded.message
                """,
                (
                    run.run_id,
                    run.loop,
                    run.status,
                    encode_timestamp(run.started_at),
                    None if run.finished_at is None else encode_timestamp(run.finished_at),
                    run.processed_count,
                    run.error_count,
                    run.message,
                ),
            )

    def latest(
        self, *, loop: Literal["fast", "slow"] | None = None, limit: int = 20
    ) -> tuple[SchedulerRun, ...]:
        where = "WHERE loop = ?" if loop is not None else ""
        params: tuple[object, ...] = (loop,) if loop is not None else ()
        with self._db.read() as connection:
            rows = connection.execute(
                f"SELECT * FROM scheduler_runs {where} "
                "ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(_run(row) for row in rows)


def _run(row: sqlite3.Row) -> SchedulerRun:
    return SchedulerRun(
        run_id=row["run_id"],
        loop=row["loop"],
        status=row["status"],
        started_at=decode_timestamp(row["started_at"]),
        finished_at=optional_timestamp(row["finished_at"]),
        processed_count=row["processed_count"],
        error_count=row["error_count"],
        message=row["message"],
    )
