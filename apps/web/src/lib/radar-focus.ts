import type { IncidentImpact, RadarEntity, RadarGraphResponse } from "@risk0/contracts"

function normalized(value: string): string {
  return value.trim().toLowerCase()
}

export function rankRadarEntities(entities: RadarEntity[], query: string): RadarEntity[] {
  const needle = normalized(query)
  if (!needle) return entities

  return entities
    .map((entity, index) => {
      const fields = [entity.label, entity.entity_id, entity.address_or_key, entity.entity_type].map(normalized)
      const rank = fields.some((field) => field === needle)
        ? 0
        : fields.some((field) => field.startsWith(needle))
          ? 1
          : fields.some((field) => field.includes(needle)) ? 2 : null
      return { entity, index, rank }
    })
    .filter((result): result is { entity: RadarEntity; index: number; rank: number } => result.rank !== null)
    .sort((left, right) => left.rank - right.rank || left.index - right.index)
    .map(({ entity }) => entity)
}

export function relevantDependencyContext(
  graph: Pick<RadarGraphResponse, "entities" | "relationships" | "similarities">,
  selectedEntityId: string | null,
  impacts: IncidentImpact[],
): ReadonlySet<string> {
  const entityIds = new Set(graph.entities.map((entity) => entity.entity_id))
  if (!selectedEntityId || !entityIds.has(selectedEntityId)) return new Set()

  const entitiesById = new Map(graph.entities.map((entity) => [entity.entity_id, entity]))
  const dependencies = new Map<string, string[]>()
  for (const relationship of graph.relationships) {
    if (relationship.relationship_type === "similar_to") continue
    if (!entityIds.has(relationship.source_entity_id) || !entityIds.has(relationship.target_entity_id)) continue
    const targets = dependencies.get(relationship.source_entity_id)
    if (targets) targets.push(relationship.target_entity_id)
    else dependencies.set(relationship.source_entity_id, [relationship.target_entity_id])
  }

  const relevant = new Set<string>([selectedEntityId])
  const queue = [selectedEntityId]
  for (const relationship of graph.relationships) {
    if (relationship.relationship_type === "similar_to" || relationship.target_entity_id !== selectedEntityId) continue
    if (entitiesById.get(relationship.source_entity_id)?.entity_type !== "position") continue
    if (!relevant.has(relationship.source_entity_id)) {
      relevant.add(relationship.source_entity_id)
      queue.push(relationship.source_entity_id)
    }
  }
  for (let index = 0; index < queue.length; index += 1) {
    for (const neighbor of dependencies.get(queue[index]) ?? []) {
      if (relevant.has(neighbor)) continue
      relevant.add(neighbor)
      queue.push(neighbor)
    }
  }

  for (const impact of impacts) {
    if (impact.target_entity_id === selectedEntityId || impact.path.some((entityId) => relevant.has(entityId))) {
      for (const entityId of impact.path) relevant.add(entityId)
    }
  }

  const similarityPairs = [
    ...graph.similarities.map((similarity) => [similarity.catalog_entity_id, similarity.candidate_entity_id] as const),
    ...graph.relationships
      .filter((relationship) => relationship.relationship_type === "similar_to")
      .map((relationship) => [relationship.source_entity_id, relationship.target_entity_id] as const),
  ]
  for (const [sourceId, targetId] of similarityPairs) {
    const sourceRelevant = relevant.has(sourceId)
    const targetRelevant = relevant.has(targetId)
    if (sourceRelevant === targetRelevant) continue
    const peerId = sourceRelevant ? targetId : sourceId
    if (entitiesById.get(peerId)?.entity_type !== "vault") continue
    if ((dependencies.get(peerId) ?? []).some((dependencyId) => relevant.has(dependencyId))) relevant.add(peerId)
  }
  return relevant
}
