import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.radar.contracts import (
    Incident,
    IncidentImpact,
    IncidentSeverity,
    IncidentStatus,
    Notification,
)
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
    optional_timestamp,
)


MAX_PAGE_LIMIT = 500
_SEVERITY_ORDER = (IncidentSeverity.WARNING, IncidentSeverity.CRITICAL)
# Below SQLite's default bound-parameter limit.
_ID_CHUNK = 500
# SQL CASE arms ranking the most severe first.
_SEVERITY_RANK = " ".join(
    f"WHEN '{severity.value}' THEN {rank}"
    for rank, severity in enumerate(reversed(_SEVERITY_ORDER))
)


@dataclass(frozen=True, slots=True)
class IncidentUpsert:
    incident: Incident
    created: bool


@dataclass(frozen=True, slots=True)
class NotificationPage:
    notifications: tuple[Notification, ...]
    next_cursor: str | None


class IncidentRepository:
    """Incident lifecycle, propagated impacts, and notification read state."""

    def __init__(self, database: RadarDatabase) -> None:
        self._db = database

    def upsert_active(self, incident: Incident) -> IncidentUpsert:
        """Open an incident, or fold the observation into the open one for its source."""
        if incident.status is not IncidentStatus.OPEN:
            raise ValueError("only open incidents can be upserted")
        with self._db.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM incidents
                WHERE source_entity_id = ? AND rule_version = ? AND status = 'OPEN'
                """,
                (incident.source_entity_id, incident.rule_version),
            ).fetchone()
            if row is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO incidents (
                            incident_id, source_entity_id, severity, status,
                            trigger_metrics, rule_version, first_observed_at,
                            last_observed_at, resolved_at, reasons
                        ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            incident.incident_id,
                            incident.source_entity_id,
                            incident.severity.value,
                            encode_json(incident.trigger_metrics),
                            incident.rule_version,
                            encode_timestamp(incident.first_observed_at),
                            encode_timestamp(incident.last_observed_at),
                            encode_strings(incident.reasons),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    if "incidents.incident_id" not in str(error):
                        raise
                    raise ValueError(
                        f"incident id {incident.incident_id} is already used"
                    ) from None
                return IncidentUpsert(incident=incident, created=True)

            existing = _incident(row)
            newer = incident.last_observed_at > existing.last_observed_at
            # A delayed observation may widen the window but never regresses metrics,
            # and severity only escalates while the incident stays open.
            merged = existing.model_copy(
                update={
                    "severity": max(
                        existing.severity, incident.severity, key=_SEVERITY_ORDER.index
                    ),
                    "first_observed_at": min(
                        existing.first_observed_at, incident.first_observed_at
                    ),
                    "last_observed_at": max(
                        existing.last_observed_at, incident.last_observed_at
                    ),
                    "trigger_metrics": (
                        incident.trigger_metrics if newer else existing.trigger_metrics
                    ),
                    "reasons": incident.reasons if newer else existing.reasons,
                }
            )
            connection.execute(
                """
                UPDATE incidents SET severity = ?, trigger_metrics = ?,
                    first_observed_at = ?, last_observed_at = ?, reasons = ?
                WHERE incident_id = ?
                """,
                (
                    merged.severity.value,
                    encode_json(merged.trigger_metrics),
                    encode_timestamp(merged.first_observed_at),
                    encode_timestamp(merged.last_observed_at),
                    encode_strings(merged.reasons),
                    merged.incident_id,
                ),
            )
        return IncidentUpsert(incident=merged, created=False)

    def resolve(self, incident_id: str, resolved_at: datetime) -> Incident | None:
        with self._db.transaction() as connection:
            connection.execute(
                """
                UPDATE incidents SET status = 'RESOLVED', resolved_at = ?
                WHERE incident_id = ? AND status = 'OPEN'
                """,
                (encode_timestamp(resolved_at), incident_id),
            )
            return self.get(incident_id)

    def resolve_many(self, incident_ids: Iterable[str], resolved_at: datetime) -> int:
        """Resolve open incidents in one transaction; returns how many changed."""
        rows = [(encode_timestamp(resolved_at), incident_id) for incident_id in incident_ids]
        if not rows:
            return 0
        with self._db.transaction() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                UPDATE incidents SET status = 'RESOLVED', resolved_at = ?
                WHERE incident_id = ? AND status = 'OPEN'
                """,
                rows,
            )
            return connection.total_changes - before

    def mark_incidents_read(self, incident_ids: Iterable[str], read_at: datetime) -> int:
        """Mark the unread notifications of these incidents read; returns how many."""
        ids = list(dict.fromkeys(incident_ids))
        changed = 0
        with self._db.transaction() as connection:
            # notifications.incident_id is unindexed: one scan per chunk of IDs
            # rather than one per ID.
            for start in range(0, len(ids), _ID_CHUNK):
                chunk = ids[start : start + _ID_CHUNK]
                changed += connection.execute(
                    f"""
                    UPDATE notifications SET read_at = ?
                    WHERE read_at IS NULL
                    AND incident_id IN ({', '.join('?' for _ in chunk)})
                    """,
                    (encode_timestamp(read_at), *chunk),
                ).rowcount
        return changed

    def get(self, incident_id: str) -> Incident | None:
        with self._db.read() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return None if row is None else _incident(row)

    def active(self, *, source_entity_id: str | None = None) -> tuple[Incident, ...]:
        return self.list_incidents(
            status=IncidentStatus.OPEN, source_entity_id=source_entity_id
        )

    def list_incidents(
        self,
        *,
        status: IncidentStatus | None = None,
        source_entity_id: str | None = None,
    ) -> tuple[Incident, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if source_entity_id is not None:
            clauses.append("source_entity_id = ?")
            params.append(source_entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM incidents {where}
                ORDER BY first_observed_at DESC, incident_id
                """,
                params,
            ).fetchall()
        return tuple(_incident(row) for row in rows)

    def top_active(
        self,
        limit: int,
        *,
        preferred_source_ids: Sequence[str] = (),
        source_ids: Sequence[str] | None = None,
    ) -> tuple[Incident, ...]:
        """The `limit` open incidents to surface first.

        Most severe first; then those whose source is preferred (e.g. on the
        radar's focus set); then the most recently opened. `source_ids`, when
        given, restricts the candidates to those sources.
        """
        source_filter = (
            ""
            if source_ids is None
            else "AND source_entity_id IN (SELECT value FROM json_each(?))"
        )
        params: list[object] = [] if source_ids is None else [json.dumps(list(source_ids))]
        with self._db.read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM incidents WHERE status = 'OPEN' {source_filter}
                ORDER BY
                    CASE severity {_SEVERITY_RANK} END,
                    source_entity_id IN (SELECT value FROM json_each(?)) DESC,
                    first_observed_at DESC,
                    incident_id
                LIMIT ?
                """,
                (*params, json.dumps(list(preferred_source_ids)), limit),
            ).fetchall()
        return tuple(_incident(row) for row in rows)

    def path_entity_ids(
        self,
        incident_ids: Sequence[str],
        *,
        paths_per_incident: int | None = None,
        preferred_target_ids: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Sources, path hops, and targets of the given incidents' impacts.

        `paths_per_incident` keeps each incident's most telling paths: revealed
        ones, then those reaching a preferred target, then the shortest and
        most confident.
        """
        if not incident_ids:
            return ()
        with self._db.read() as connection:
            rows = connection.execute(
                """
                WITH chosen (incident_id) AS (SELECT value FROM json_each(?)),
                preferred (entity_id) AS (SELECT value FROM json_each(?)),
                ranked AS (
                    SELECT i.target_entity_id, i.path, ROW_NUMBER() OVER (
                        PARTITION BY i.incident_id ORDER BY
                            i.revealed DESC,
                            i.target_entity_id IN preferred DESC,
                            i.distance,
                            -- Confidence is a decimal string; REAL only ranks it.
                            CAST(i.confidence AS REAL) DESC,
                            i.target_entity_id
                    ) AS position
                    FROM chosen JOIN incident_impacts i USING (incident_id)
                ),
                kept AS (SELECT * FROM ranked WHERE ? IS NULL OR position <= ?)
                SELECT source_entity_id FROM chosen JOIN incidents USING (incident_id)
                UNION SELECT target_entity_id FROM kept
                UNION SELECT hop.value FROM kept, json_each(kept.path) AS hop
                """,
                (
                    json.dumps(list(incident_ids)),
                    json.dumps(list(preferred_target_ids)),
                    paths_per_incident,
                    paths_per_incident,
                ),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def replace_impacts(
        self, incident_id: str, impacts: Iterable[IncidentImpact]
    ) -> int:
        values = [impact_values(impact) for impact in impacts]
        for impact in values:
            if impact[0] != incident_id:
                raise ValueError(f"impact of {impact[0]} cannot be stored under {incident_id}")
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM incident_impacts WHERE incident_id = ?", (incident_id,)
            )
            connection.executemany(
                f"INSERT INTO incident_impacts ({IMPACT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def impacts(self, incident_id: str) -> tuple[IncidentImpact, ...]:
        with self._db.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM incident_impacts WHERE incident_id = ?
                ORDER BY distance, target_entity_id
                """,
                (incident_id,),
            ).fetchall()
        return tuple(impact_from_row(row) for row in rows)

    def add_notification(self, notification: Notification) -> bool:
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    notification_id, incident_id, severity, title, message,
                    created_at, read_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification.notification_id,
                    notification.incident_id,
                    notification.severity.value,
                    notification.title,
                    notification.message,
                    encode_timestamp(notification.created_at),
                    None
                    if notification.read_at is None
                    else encode_timestamp(notification.read_at),
                ),
            )
        return cursor.rowcount == 1

    def get_notification(self, notification_id: str) -> Notification | None:
        with self._db.read() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        return None if row is None else _notification(row)

    def list_notifications(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        unread_only: bool = False,
    ) -> NotificationPage:
        if not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
        clauses: list[str] = []
        params: list[object] = []
        if unread_only:
            clauses.append("read_at IS NULL")
        if cursor is not None:
            clauses.append("(created_at, notification_id) < (?, ?)")
            params.extend(decode_cursor(cursor))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db.read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notifications {where}
                ORDER BY created_at DESC, notification_id DESC LIMIT ?
                """,
                (*params, limit + 1),
            ).fetchall()
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1]["created_at"], page[-1]["notification_id"])
            if len(rows) > limit
            else None
        )
        return NotificationPage(
            notifications=tuple(_notification(row) for row in page),
            next_cursor=next_cursor,
        )

    def mark_read(self, notification_id: str, read_at: datetime) -> Notification | None:
        with self._db.transaction() as connection:
            connection.execute(
                """
                UPDATE notifications SET read_at = ?
                WHERE notification_id = ? AND read_at IS NULL
                """,
                (encode_timestamp(read_at), notification_id),
            )
            return self.get_notification(notification_id)

    def mark_all_read(self, read_at: datetime) -> int:
        """Mark every unread notification read; returns how many changed."""
        with self._db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                (encode_timestamp(read_at),),
            )
        return cursor.rowcount


IMPACT_COLUMNS = (
    "incident_id, target_entity_id, path, distance, confidence, "
    "estimated_affected_amount, reasons, revealed, alternate_path_count"
)


def impact_values(impact: IncidentImpact) -> tuple[object, ...]:
    return (
        impact.incident_id,
        impact.target_entity_id,
        encode_strings(impact.path),
        impact.distance,
        encode_decimal(impact.confidence),
        encode_decimal(impact.estimated_affected_amount),
        encode_strings(impact.reasons),
        int(impact.revealed),
        impact.alternate_path_count,
    )


def impact_from_row(row: sqlite3.Row) -> IncidentImpact:
    return IncidentImpact(
        incident_id=row["incident_id"],
        target_entity_id=row["target_entity_id"],
        path=decode_strings(row["path"]),
        distance=row["distance"],
        confidence=decode_decimal(row["confidence"]),
        estimated_affected_amount=decode_decimal(row["estimated_affected_amount"]),
        reasons=decode_strings(row["reasons"]),
        revealed=bool(row["revealed"]),
        alternate_path_count=row["alternate_path_count"],
    )


def _incident(row: sqlite3.Row) -> Incident:
    return Incident(
        incident_id=row["incident_id"],
        source_entity_id=row["source_entity_id"],
        severity=IncidentSeverity(row["severity"]),
        status=IncidentStatus(row["status"]),
        trigger_metrics=decode_json(row["trigger_metrics"]),
        rule_version=row["rule_version"],
        first_observed_at=decode_timestamp(row["first_observed_at"]),
        last_observed_at=decode_timestamp(row["last_observed_at"]),
        resolved_at=optional_timestamp(row["resolved_at"]),
        reasons=decode_strings(row["reasons"]),
    )


def _notification(row: sqlite3.Row) -> Notification:
    return Notification(
        notification_id=row["notification_id"],
        incident_id=row["incident_id"],
        severity=IncidentSeverity(row["severity"]),
        title=row["title"],
        message=row["message"],
        created_at=decode_timestamp(row["created_at"]),
        read_at=optional_timestamp(row["read_at"]),
    )

