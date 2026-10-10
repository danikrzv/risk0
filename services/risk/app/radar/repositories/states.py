import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

from app.contracts.models import FreshnessStatus, MetricValue
from app.radar.contracts import HealthState, HealthStatus, RadarEntityType, StateSnapshot
from app.radar.repositories.database import (
    RadarDatabase,
    decode_cursor,
    decode_decimal,
    decode_json,
    decode_strings,
    decode_timestamp,
    encode_cursor,
    encode_decimal,
    encode_json,
    encode_strings,
    encode_timestamp,
)


RAW_RETENTION = timedelta(days=30)
HOURLY_RETENTION = timedelta(days=365)
MAX_HISTORY_LIMIT = 1000
_FRESHNESS_ORDER = (
    FreshnessStatus.FRESH,
    FreshnessStatus.STALE_USABLE,
    FreshnessStatus.EXPIRED,
)


@dataclass(frozen=True, slots=True)
class StateHistory:
    snapshots: tuple[StateSnapshot, ...]
    health_states: tuple[HealthState, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class HourlyAggregate:
    entity_id: str
    state_type: RadarEntityType
    hour_start: datetime
    sample_count: int
    worst_freshness: FreshnessStatus
    # Numeric metrics map to min/max/mean/last/count; other metrics keep only last.
    metrics: dict[str, dict[str, MetricValue]]
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class StatePruneResult:
    removed_snapshots: int
    removed_hourly_aggregates: int
    removed_health_states: int


class StateRepository:
    """Time-varying snapshots, health evaluations, and their retention."""

    def __init__(self, database: RadarDatabase) -> None:
        self._db = database

    def append_snapshot(self, snapshot: StateSnapshot) -> bool:
        """Persist a snapshot unless it duplicates or predates the latest one."""
        observed = encode_timestamp(snapshot.observed_at)
        with self._db.transaction() as connection:
            latest = connection.execute(
                """
                SELECT MAX(observed_at) FROM state_snapshots
                WHERE entity_id = ? AND state_type = ? AND source_identity = ?
                """,
                (snapshot.entity_id, snapshot.state_type.value, snapshot.source_identity),
            ).fetchone()[0]
            if latest is not None and latest >= observed:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO state_snapshots (
                    snapshot_id, entity_id, state_type, observed_at, retrieved_at,
                    source, source_identity, freshness, metrics, missing_fields
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.entity_id,
                    snapshot.state_type.value,
                    observed,
                    encode_timestamp(snapshot.retrieved_at),
                    snapshot.source,
                    snapshot.source_identity,
                    snapshot.freshness.value,
                    encode_json(snapshot.metrics),
                    encode_strings(snapshot.missing_fields),
                ),
            )
        return cursor.rowcount == 1

    def latest_snapshot(
        self,
        entity_id: str,
        *,
        source_identity: str | None = None,
        state_type: RadarEntityType | None = None,
    ) -> StateSnapshot | None:
        clauses = ["entity_id = ?"]
        params: list[object] = [entity_id]
        if source_identity is not None:
            clauses.append("source_identity = ?")
            params.append(source_identity)
        if state_type is not None:
            clauses.append("state_type = ?")
            params.append(state_type.value)
        with self._db.transaction() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM state_snapshots WHERE {' AND '.join(clauses)}
                ORDER BY observed_at DESC, retrieved_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return None if row is None else _snapshot(row)

    def set_health(self, state: HealthState) -> bool:
        """Record an evaluation; delayed ones join history but never become latest."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO health_states (
                    entity_id, evaluated_at, status, score, reasons, missing_fields,
                    rule_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.entity_id,
                    encode_timestamp(state.evaluated_at),
                    state.status.value,
                    encode_decimal(state.score),
                    encode_strings(state.reasons),
                    encode_strings(state.missing_fields),
                    state.rule_version,
                ),
            )
        return cursor.rowcount == 1

    def latest_health(self, entity_id: str) -> HealthState | None:
        with self._db.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM health_states WHERE entity_id = ?
                ORDER BY evaluated_at DESC LIMIT 1
                """,
                (entity_id,),
            ).fetchone()
        return None if row is None else _health(row)

    def latest_health_all(self) -> dict[str, HealthState]:
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT h.* FROM health_states h
                JOIN (
                    SELECT entity_id, MAX(evaluated_at) AS evaluated_at
                    FROM health_states GROUP BY entity_id
                ) latest USING (entity_id, evaluated_at)
                ORDER BY h.entity_id
                """
            ).fetchall()
        return {row["entity_id"]: _health(row) for row in rows}

    def history(
        self, entity_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> StateHistory:
        """Newest-first snapshots with the health evaluations of the same time window."""
        if not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
        upper = decode_cursor(cursor) if cursor is not None else None
        snapshot_sql = "SELECT * FROM state_snapshots WHERE entity_id = ?"
        params: list[object] = [entity_id]
        if upper is not None:
            snapshot_sql += " AND (observed_at, snapshot_id) < (?, ?)"
            params.extend(upper)
        snapshot_sql += " ORDER BY observed_at DESC, snapshot_id DESC LIMIT ?"
        params.append(limit + 1)
        with self._db.transaction() as connection:
            rows = connection.execute(snapshot_sql, params).fetchall()
            page = rows[:limit]
            has_more = len(rows) > limit
            health_sql = "SELECT * FROM health_states WHERE entity_id = ?"
            health_params: list[object] = [entity_id]
            if upper is not None:
                health_sql += " AND evaluated_at < ?"
                health_params.append(upper[0])
            if has_more:
                health_sql += " AND evaluated_at >= ?"
                health_params.append(page[-1]["observed_at"])
            health_rows = connection.execute(
                health_sql + " ORDER BY evaluated_at DESC", health_params
            ).fetchall()
        next_cursor = (
            encode_cursor(page[-1]["observed_at"], page[-1]["snapshot_id"]) if has_more else None
        )
        return StateHistory(
            snapshots=tuple(_snapshot(row) for row in page),
            health_states=tuple(_health(row) for row in health_rows),
            next_cursor=next_cursor,
        )

    def aggregate_hour(self, at: datetime) -> int:
        """(Re)build the hourly aggregates of the UTC hour containing `at`."""
        hour_start = _floor_hour(at)
        start = encode_timestamp(hour_start)
        end = encode_timestamp(hour_start + timedelta(hours=1))
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM state_snapshots
                WHERE observed_at >= ? AND observed_at < ?
                ORDER BY entity_id, state_type, observed_at, snapshot_id
                """,
                (start, end),
            ).fetchall()
            groups: dict[tuple[str, str], list[StateSnapshot]] = defaultdict(list)
            for row in rows:
                groups[(row["entity_id"], row["state_type"])].append(_snapshot(row))
            connection.executemany(
                """
                INSERT INTO state_hourly_aggregates (
                    entity_id, state_type, hour_start, sample_count, worst_freshness,
                    metrics, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (entity_id, state_type, hour_start) DO UPDATE SET
                    sample_count = excluded.sample_count,
                    worst_freshness = excluded.worst_freshness,
                    metrics = excluded.metrics,
                    first_observed_at = excluded.first_observed_at,
                    last_observed_at = excluded.last_observed_at
                """,
                [
                    (
                        entity_id,
                        state_type,
                        start,
                        len(samples),
                        _worst_freshness(samples).value,
                        encode_json(_aggregate_metrics(samples)),
                        encode_timestamp(samples[0].observed_at),
                        encode_timestamp(samples[-1].observed_at),
                    )
                    for (entity_id, state_type), samples in groups.items()
                ],
            )
        return len(groups)

    def hourly(
        self, entity_id: str, *, start: datetime, end: datetime
    ) -> tuple[HourlyAggregate, ...]:
        with self._db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM state_hourly_aggregates
                WHERE entity_id = ? AND hour_start >= ? AND hour_start < ?
                ORDER BY hour_start, state_type
                """,
                (entity_id, encode_timestamp(start), encode_timestamp(end)),
            ).fetchall()
        return tuple(
            HourlyAggregate(
                entity_id=row["entity_id"],
                state_type=RadarEntityType(row["state_type"]),
                hour_start=decode_timestamp(row["hour_start"]),
                sample_count=row["sample_count"],
                worst_freshness=FreshnessStatus(row["worst_freshness"]),
                metrics=decode_json(row["metrics"]),
                first_observed_at=decode_timestamp(row["first_observed_at"]),
                last_observed_at=decode_timestamp(row["last_observed_at"]),
            )
            for row in rows
        )

    def prune(self, now: datetime) -> StatePruneResult:
        """Apply 30-day raw and one-year hourly retention; incidents are never pruned."""
        # Raw rows are removed in whole hours so each hour is summarized from complete data.
        raw_cutoff = _floor_hour(now - RAW_RETENTION)
        hourly_cutoff = now - HOURLY_RETENTION
        raw_value = encode_timestamp(raw_cutoff)
        with self._db.transaction() as connection:
            expiring = connection.execute(
                """
                SELECT DISTINCT observed_at FROM state_snapshots
                WHERE observed_at < ? AND observed_at >= ?
                """,
                (raw_value, encode_timestamp(_floor_hour(hourly_cutoff))),
            ).fetchall()
            for hour in sorted({_floor_hour(decode_timestamp(row[0])) for row in expiring}):
                self.aggregate_hour(hour)
            removed_snapshots = connection.execute(
                "DELETE FROM state_snapshots WHERE observed_at < ?", (raw_value,)
            ).rowcount
            removed_hourly = connection.execute(
                "DELETE FROM state_hourly_aggregates WHERE hour_start < ?",
                (encode_timestamp(hourly_cutoff),),
            ).rowcount
            # The latest evaluation of each entity is kept regardless of age.
            removed_health = connection.execute(
                """
                DELETE FROM health_states
                WHERE evaluated_at < ? AND evaluated_at < (
                    SELECT MAX(latest.evaluated_at) FROM health_states latest
                    WHERE latest.entity_id = health_states.entity_id
                )
                """,
                (encode_timestamp(now - RAW_RETENTION),),
            ).rowcount
        return StatePruneResult(
            removed_snapshots=removed_snapshots,
            removed_hourly_aggregates=removed_hourly,
            removed_health_states=removed_health,
        )


def _snapshot(row: sqlite3.Row) -> StateSnapshot:
    return StateSnapshot(
        snapshot_id=row["snapshot_id"],
        entity_id=row["entity_id"],
        state_type=RadarEntityType(row["state_type"]),
        observed_at=decode_timestamp(row["observed_at"]),
        retrieved_at=decode_timestamp(row["retrieved_at"]),
        source=row["source"],
        source_identity=row["source_identity"],
        freshness=FreshnessStatus(row["freshness"]),
        metrics=decode_json(row["metrics"]),
        missing_fields=decode_strings(row["missing_fields"]),
    )


def _health(row: sqlite3.Row) -> HealthState:
    return HealthState(
        entity_id=row["entity_id"],
        status=HealthStatus(row["status"]),
        score=decode_decimal(row["score"]),
        reasons=decode_strings(row["reasons"]),
        missing_fields=decode_strings(row["missing_fields"]),
        evaluated_at=decode_timestamp(row["evaluated_at"]),
        rule_version=row["rule_version"],
    )



def _floor_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("radar timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _worst_freshness(samples: Sequence[StateSnapshot]) -> FreshnessStatus:
    return max(
        (sample.freshness for sample in samples), key=_FRESHNESS_ORDER.index
    )


def _aggregate_metrics(
    samples: Sequence[StateSnapshot],
) -> dict[str, dict[str, MetricValue]]:
    keys = sorted({key for sample in samples for key in sample.metrics})
    aggregates: dict[str, dict[str, MetricValue]] = {}
    for key in keys:
        series = [sample.metrics.get(key) for sample in samples]
        present = [value for value in series if value is not None]
        last = series[-1]
        if any(not isinstance(value, Decimal) for value in present):
            aggregates[key] = {"last": last}
            continue
        # Missing samples are excluded rather than counted as zero.
        aggregates[key] = {
            "min": min(present) if present else None,
            "max": max(present) if present else None,
            "mean": _mean(present),
            "last": last,
            "count": Decimal(len(present)),
        }
    return aggregates


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 60
        return sum(values, Decimal(0)) / len(values)
