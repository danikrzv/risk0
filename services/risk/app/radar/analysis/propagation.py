from collections import defaultdict, deque
from decimal import Decimal

from app.radar.contracts import (
    Incident,
    IncidentImpact,
    IncidentStatus,
    RadarEntity,
    RadarRelationship,
    RadarRelationshipType,
    SimilarityRecord,
    SIMILARITY_STORAGE_THRESHOLD,
)


NON_PROPAGATING = {RadarRelationshipType.SIMILAR_TO}


class PropagationEngine:
    def propagate(
        self,
        incident: Incident,
        entities: tuple[RadarEntity, ...],
        relationships: tuple[RadarRelationship, ...],
        similarities: tuple[SimilarityRecord, ...],
    ) -> tuple[IncidentImpact, ...]:
        if incident.status is not IncidentStatus.OPEN:
            return ()
        entity_by_id = {entity.entity_id: entity for entity in entities}
        if incident.source_entity_id not in entity_by_id:
            return ()
        adjacency: dict[str, list[tuple[str, RadarRelationship]]] = defaultdict(list)
        for relationship in relationships:
            if relationship.relationship_type in NON_PROPAGATING:
                continue
            if relationship.source_entity_id not in entity_by_id or relationship.target_entity_id not in entity_by_id:
                continue
            adjacency[relationship.target_entity_id].append(
                (relationship.source_entity_id, relationship)
            )
        for targets in adjacency.values():
            targets.sort(key=lambda item: (item[0], item[1].relationship_type.value))

        source = incident.source_entity_id
        paths: dict[str, tuple[str, ...]] = {source: (source,)}
        alternate_counts: dict[str, int] = defaultdict(int)
        queue: deque[str] = deque((source,))
        while queue:
            current = queue.popleft()
            for target, _relationship in adjacency.get(current, ()):
                candidate = (*paths[current], target)
                if target not in paths:
                    paths[target] = candidate
                    queue.append(target)
                elif candidate != paths[target]:
                    alternate_counts[target] += 1

        revealable = {
            item.candidate_entity_id
            for item in similarities
            if item.score > SIMILARITY_STORAGE_THRESHOLD
        }
        impacts: list[IncidentImpact] = []
        for target, path in paths.items():
            if target == source:
                continue
            entity = entity_by_id[target]
            distance = len(path) - 1
            impacts.append(
                IncidentImpact(
                    incident_id=incident.incident_id,
                    target_entity_id=target,
                    path=path,
                    distance=distance,
                    confidence=max(Decimal("0.50"), Decimal(1) - Decimal("0.05") * distance),
                    estimated_affected_amount=None,
                    reasons=(f"propagated across {distance} dependency edge(s)",),
                    revealed=not entity.visible and target in revealable,
                    alternate_path_count=alternate_counts[target],
                )
            )
        return tuple(sorted(impacts, key=lambda item: (item.distance, item.target_entity_id)))
