"""Typed, deterministic incident propagation over the radar graph.

Relationships point from a dependent to its dependency (``market --uses_oracle-->
oracle``), so an incident travels each edge in reverse: from the failing entity
to everything that depends on it. Traversal is a layered breadth-first search
from the standard library:

* ``similar_to`` and ``exits_through`` edges never carry an incident, and an
  edge with an explicit zero weight (an emptied allocation) carries nothing.
* Each node keeps its shortest path; among equally short paths the highest
  confidence wins, then the lexicographically smallest path (determinism).
* Confidence is the product of per-edge confidences. Immutable Morpho market
  parameters (oracle, collateral, IRM) and declared positions are certain;
  vault allocations change between cycles; protocol/chain links are broad.
* ``alternate_path_count`` is the number of other affected neighbours the
  impact also arrives through, each giving a distinct simple path
  (that neighbour's own path plus the final hop). It is capped.
* A hidden entity whose stored similarity exceeds 33% is revealed when the
  incident reaches it or originates at it; every impact whose path crosses a
  revealed entity is flagged ``revealed``. Unreached hidden entities stay hidden.
"""

from collections import defaultdict
from decimal import Decimal

from app.radar.contracts import (
    SIMILARITY_STORAGE_THRESHOLD,
    Incident,
    IncidentImpact,
    IncidentStatus,
    RadarEntity,
    RadarRelationship,
    RadarRelationshipType,
    SimilarityRecord,
)


NON_PROPAGATING = frozenset(
    {RadarRelationshipType.SIMILAR_TO, RadarRelationshipType.EXITS_THROUGH}
)
EDGE_CONFIDENCE = {
    RadarRelationshipType.USES_ORACLE: Decimal("1"),
    RadarRelationshipType.USES_COLLATERAL: Decimal("1"),
    RadarRelationshipType.USES_IRM: Decimal("1"),
    RadarRelationshipType.LENDS: Decimal("1"),
    RadarRelationshipType.BORROWS: Decimal("1"),
    RadarRelationshipType.ALLOCATED_TO: Decimal("0.95"),
    RadarRelationshipType.BACKED_BY: Decimal("0.90"),
    RadarRelationshipType.DEPLOYED_ON: Decimal("0.90"),
}
UNWEIGHTED_ALLOCATION_CONFIDENCE = Decimal("0.90")
MIN_CONFIDENCE = Decimal("0.50")
CONFIDENCE_QUANTUM = Decimal("0.0001")
MAX_ALTERNATE_PATHS = 1000

Adjacency = dict[str, tuple[tuple[str, Decimal], ...]]
Incoming = dict[str, tuple[str, ...]]


class PropagationEngine:
    def __init__(self) -> None:
        # One-entry cache: callers propagate every active incident over the same
        # topology tuples, so the adjacency map is built once per cycle.
        self._cache_key: tuple[object, object] | None = None
        self._cache: tuple[dict[str, RadarEntity], Adjacency, Incoming] | None = None

    def propagate(
        self,
        incident: Incident,
        entities: tuple[RadarEntity, ...],
        relationships: tuple[RadarRelationship, ...],
        similarities: tuple[SimilarityRecord, ...],
    ) -> tuple[IncidentImpact, ...]:
        if incident.status is not IncidentStatus.OPEN:
            return ()
        entity_by_id, adjacency, incoming = self._topology(entities, relationships)
        source = incident.source_entity_id
        if source not in entity_by_id:
            return ()

        paths, confidences = _layered_bfs(source, adjacency)
        revealable = {
            item.candidate_entity_id
            for item in similarities
            if item.score > SIMILARITY_STORAGE_THRESHOLD
        }
        revealed_nodes = {
            node
            for node in paths
            if node in revealable and _hidden(entity_by_id[node])
        }

        impacts: list[IncidentImpact] = []
        if source in revealed_nodes:
            impacts.append(
                IncidentImpact(
                    incident_id=incident.incident_id,
                    target_entity_id=source,
                    path=(source,),
                    distance=0,
                    confidence=Decimal(1),
                    estimated_affected_amount=None,
                    reasons=("incident originates at a hidden monitored neighbor",),
                    revealed=True,
                )
            )
        for target, path in paths.items():
            if target == source:
                continue
            distance = len(path) - 1
            revealed = any(node in revealed_nodes for node in path)
            reasons = [f"propagated across {distance} dependency edge(s)"]
            if target in revealed_nodes:
                reasons.append("incident reached a hidden monitored neighbor")
            elif revealed:
                reasons.append("path crosses a revealed hidden neighbor")
            impacts.append(
                IncidentImpact(
                    incident_id=incident.incident_id,
                    target_entity_id=target,
                    path=path,
                    distance=distance,
                    confidence=max(MIN_CONFIDENCE, confidences[target]).quantize(CONFIDENCE_QUANTUM),
                    estimated_affected_amount=None,
                    reasons=tuple(reasons),
                    revealed=revealed,
                    alternate_path_count=_alternate_arrivals(target, path, paths, incoming),
                )
            )
        return tuple(sorted(impacts, key=lambda item: (item.distance, item.target_entity_id)))

    def _topology(
        self,
        entities: tuple[RadarEntity, ...],
        relationships: tuple[RadarRelationship, ...],
    ) -> tuple[dict[str, RadarEntity], Adjacency, Incoming]:
        if (
            self._cache is not None
            and self._cache_key is not None
            and self._cache_key[0] is entities
            and self._cache_key[1] is relationships
        ):
            return self._cache
        entity_by_id = {entity.entity_id: entity for entity in entities}
        edges: dict[str, dict[str, Decimal]] = defaultdict(dict)
        for relationship in relationships:
            confidence = _edge_confidence(relationship)
            if confidence is None:
                continue
            dependency = relationship.target_entity_id
            dependent = relationship.source_entity_id
            if dependency not in entity_by_id or dependent not in entity_by_id:
                continue
            if dependency == dependent:
                continue
            # Parallel edges between one pair collapse to the most confident one.
            edges[dependency][dependent] = max(
                confidence, edges[dependency].get(dependent, Decimal(0))
            )
        adjacency = {
            node: tuple(sorted(targets.items())) for node, targets in edges.items()
        }
        reverse: dict[str, list[str]] = defaultdict(list)
        for node, targets in adjacency.items():
            for target, _confidence in targets:
                reverse[target].append(node)
        incoming = {node: tuple(sorted(sources)) for node, sources in reverse.items()}
        # Tuples are immutable and referenced by the key, so identity is safe.
        self._cache_key = (entities, relationships)
        self._cache = (entity_by_id, adjacency, incoming)
        return self._cache


def _edge_confidence(relationship: RadarRelationship) -> Decimal | None:
    kind = relationship.relationship_type
    if kind in NON_PROPAGATING:
        return None
    if relationship.weight is not None and relationship.weight == 0:
        return None
    if kind is RadarRelationshipType.ALLOCATED_TO and relationship.weight is None:
        return UNWEIGHTED_ALLOCATION_CONFIDENCE
    return EDGE_CONFIDENCE.get(kind)


def _hidden(entity: RadarEntity) -> bool:
    # An entity revealed by an earlier cycle is still a hidden neighbor.
    return not entity.visible or entity.metadata.get("visibility_reason") == "incident_reveal"


def _layered_bfs(
    source: str, adjacency: Adjacency
) -> tuple[dict[str, tuple[str, ...]], dict[str, Decimal]]:
    paths: dict[str, tuple[str, ...]] = {source: (source,)}
    confidences: dict[str, Decimal] = {source: Decimal(1)}
    frontier = [source]
    while frontier:
        best: dict[str, tuple[Decimal, tuple[str, ...]]] = {}
        for node in frontier:
            for target, edge_confidence in adjacency.get(node, ()):
                if target in paths:
                    continue
                candidate = (confidences[node] * edge_confidence, (*paths[node], target))
                kept = best.get(target)
                if kept is None or (-candidate[0], candidate[1]) < (-kept[0], kept[1]):
                    best[target] = candidate
        for target, (confidence, path) in best.items():
            paths[target] = path
            confidences[target] = confidence
        frontier = sorted(best)
    return paths, confidences


def _alternate_arrivals(
    target: str,
    path: tuple[str, ...],
    paths: dict[str, tuple[str, ...]],
    incoming: Incoming,
) -> int:
    predecessor = path[-2]
    count = 0
    for node in incoming.get(target, ()):
        node_path = paths.get(node)
        if node == predecessor or node_path is None or target in node_path:
            continue
        count += 1
        if count >= MAX_ALTERNATE_PATHS:
            break
    return count
