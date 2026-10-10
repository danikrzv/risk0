import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime

from app.radar.contracts import (
    RadarEntity,
    RadarEntityType,
    RadarRelationship,
    RadarRelationshipType,
    SimilarityRecord,
)
from app.radar.repositories.database import (
    RadarDatabase,
    decode_decimal,
    decode_json,
    decode_strings,
    decode_timestamp,
    encode_decimal,
    encode_json,
    encode_strings,
    encode_timestamp,
)


class TopologyRepository:
    """Entities, relationships, and similarities discovered by the slow loop."""

    def __init__(self, database: RadarDatabase) -> None:
        self._db = database

    def upsert_entities(
        self, entities: Iterable[RadarEntity], *, seen_at: datetime
    ) -> int:
        seen = encode_timestamp(seen_at)
        rows = [
            (
                entity.entity_id,
                entity.entity_type.value,
                entity.chain_id,
                entity.address_or_key,
                entity.label,
                entity.source,
                int(entity.catalog),
                int(entity.visible),
                encode_json(entity.metadata),
                seen,
                seen,
            )
            for entity in entities
        ]
        with self._db.transaction() as connection:
            # ON CONFLICT updates in place: a REPLACE would delete the row and break
            # every snapshot, incident, and similarity that references it.
            connection.executemany(
                """
                INSERT INTO entities (
                    entity_id, entity_type, chain_id, address_or_key, label, source,
                    catalog, visible, metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (entity_id) DO UPDATE SET
                    label = excluded.label,
                    source = excluded.source,
                    catalog = excluded.catalog,
                    visible = excluded.visible,
                    metadata = excluded.metadata,
                    last_seen_at = excluded.last_seen_at,
                    tombstoned_at = NULL
                WHERE excluded.last_seen_at >= entities.last_seen_at
                """,
                rows,
            )
        return len(rows)

    def tombstone_missing(
        self,
        *,
        seen_entity_ids: Iterable[str],
        chain_ids: Iterable[int],
        at: datetime,
    ) -> int:
        """Tombstone live entities of fully discovered chains that were not seen again."""
        seen = set(seen_entity_ids)
        chains = tuple(chain_ids)
        if not chains:
            return 0
        timestamp = encode_timestamp(at)
        with self._db.transaction() as connection:
            candidates = connection.execute(
                f"""
                SELECT entity_id FROM entities
                WHERE tombstoned_at IS NULL AND last_seen_at < ?
                AND chain_id IN ({_placeholders(chains)})
                """,
                (timestamp, *chains),
            ).fetchall()
            missing = [(timestamp, row[0]) for row in candidates if row[0] not in seen]
            connection.executemany(
                "UPDATE entities SET tombstoned_at = ? WHERE entity_id = ?", missing
            )
            connection.executemany(
                """
                UPDATE relationships SET tombstoned_at = ?
                WHERE tombstoned_at IS NULL
                AND (source_entity_id = ? OR target_entity_id = ?)
                """,
                [(timestamp, entity_id, entity_id) for _, entity_id in missing],
            )
        return len(missing)

    def get_entity(
        self, entity_id: str, *, include_tombstoned: bool = False
    ) -> RadarEntity | None:
        found = self.entities(entity_ids=(entity_id,), include_tombstoned=include_tombstoned)
        return found[0] if found else None

    def first_seen_at(self, entity_id: str) -> datetime | None:
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT first_seen_at FROM entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        return None if row is None else decode_timestamp(row[0])

    def entities(
        self,
        *,
        chain_id: int | None = None,
        entity_type: RadarEntityType | None = None,
        visible: bool | None = None,
        catalog: bool | None = None,
        entity_ids: Sequence[str] | None = None,
        include_tombstoned: bool = False,
    ) -> tuple[RadarEntity, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_tombstoned:
            clauses.append("tombstoned_at IS NULL")
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type.value)
        if visible is not None:
            clauses.append("visible = ?")
            params.append(int(visible))
        if catalog is not None:
            clauses.append("catalog = ?")
            params.append(int(catalog))
        if entity_ids is not None:
            if not entity_ids:
                return ()
            clauses.append(f"entity_id IN ({_placeholders(entity_ids)})")
            params.extend(entity_ids)
        with self._db.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM entities {_where(clauses)} ORDER BY entity_id", params
            ).fetchall()
        return tuple(_entity(row) for row in rows)

    def upsert_relationships(
        self, relationships: Iterable[RadarRelationship], *, seen_at: datetime
    ) -> int:
        seen = encode_timestamp(seen_at)
        rows = [
            (
                relationship.source_entity_id,
                relationship.target_entity_id,
                relationship.relationship_type.value,
                encode_decimal(relationship.weight),
                encode_json(relationship.metadata),
                seen,
                seen,
            )
            for relationship in relationships
        ]
        with self._db.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO relationships (
                    source_entity_id, target_entity_id, relationship_type, weight,
                    metadata, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_entity_id, target_entity_id, relationship_type)
                DO UPDATE SET
                    weight = excluded.weight,
                    metadata = excluded.metadata,
                    last_seen_at = excluded.last_seen_at,
                    tombstoned_at = NULL
                WHERE excluded.last_seen_at >= relationships.last_seen_at
                """,
                rows,
            )
        return len(rows)

    def relationships(
        self,
        *,
        entity_ids: Sequence[str] | None = None,
        relationship_type: RadarRelationshipType | None = None,
        include_tombstoned: bool = False,
    ) -> tuple[RadarRelationship, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_tombstoned:
            clauses.append("tombstoned_at IS NULL")
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type.value)
        if entity_ids is not None:
            if not entity_ids:
                return ()
            marks = _placeholders(entity_ids)
            clauses.append(
                f"(source_entity_id IN ({marks}) OR target_entity_id IN ({marks}))"
            )
            params.extend((*entity_ids, *entity_ids))
        with self._db.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM relationships {_where(clauses)}
                ORDER BY source_entity_id, target_entity_id, relationship_type
                """,
                params,
            ).fetchall()
        return tuple(_relationship(row) for row in rows)

    def replace_similarities(
        self, catalog_entity_id: str, records: Iterable[SimilarityRecord]
    ) -> int:
        """Replace one catalog vault's neighbors; only records flagged `stored` persist."""
        stored = [record for record in records if record.stored]
        for record in stored:
            if record.catalog_entity_id != catalog_entity_id:
                raise ValueError(
                    f"similarity for {record.catalog_entity_id} cannot be stored "
                    f"under {catalog_entity_id}"
                )
        with self._db.transaction() as connection:
            connection.execute(
                "DELETE FROM similarities WHERE catalog_entity_id = ?",
                (catalog_entity_id,),
            )
            connection.executemany(
                """
                INSERT INTO similarities (
                    catalog_entity_id, candidate_entity_id, model_version, score,
                    components, missing_dimensions, normally_visible, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.catalog_entity_id,
                        record.candidate_entity_id,
                        record.model_version,
                        encode_decimal(record.score),
                        encode_json(record.components),
                        encode_strings(record.missing_dimensions),
                        int(record.normally_visible),
                        encode_timestamp(record.computed_at),
                    )
                    for record in stored
                ],
            )
        return len(stored)

    def similarities(
        self,
        *,
        catalog_entity_id: str | None = None,
        candidate_entity_id: str | None = None,
    ) -> tuple[SimilarityRecord, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if catalog_entity_id is not None:
            clauses.append("catalog_entity_id = ?")
            params.append(catalog_entity_id)
        if candidate_entity_id is not None:
            clauses.append("candidate_entity_id = ?")
            params.append(candidate_entity_id)
        with self._db.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM similarities {_where(clauses)}", params
            ).fetchall()
        records = [_similarity(row) for row in rows]
        # Scores are exact decimal strings, so ordering happens on Decimals here.
        records.sort(
            key=lambda record: (
                record.catalog_entity_id,
                -record.score,
                record.candidate_entity_id,
            )
        )
        return tuple(records)


def _entity(row: sqlite3.Row) -> RadarEntity:
    return RadarEntity(
        entity_id=row["entity_id"],
        entity_type=RadarEntityType(row["entity_type"]),
        chain_id=row["chain_id"],
        address_or_key=row["address_or_key"],
        label=row["label"],
        source=row["source"],
        catalog=bool(row["catalog"]),
        visible=bool(row["visible"]),
        metadata=decode_json(row["metadata"]),
    )


def _relationship(row: sqlite3.Row) -> RadarRelationship:
    return RadarRelationship(
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relationship_type=RadarRelationshipType(row["relationship_type"]),
        weight=decode_decimal(row["weight"]),
        metadata=decode_json(row["metadata"]),
    )


def _similarity(row: sqlite3.Row) -> SimilarityRecord:
    return SimilarityRecord(
        catalog_entity_id=row["catalog_entity_id"],
        candidate_entity_id=row["candidate_entity_id"],
        score=decode_decimal(row["score"]),
        components=decode_json(row["components"]),
        missing_dimensions=decode_strings(row["missing_dimensions"]),
        model_version=row["model_version"],
        stored=True,
        normally_visible=bool(row["normally_visible"]),
        computed_at=decode_timestamp(row["computed_at"]),
    )


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def _where(clauses: Sequence[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""
