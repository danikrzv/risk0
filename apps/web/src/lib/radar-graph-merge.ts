import type { RadarGraphResponse } from "@risk0/contracts"

/** The topology parts of a radar, incident, or focus graph response. */
export type RadarTopology = Pick<RadarGraphResponse, "entities" | "relationships" | "similarities">

/**
 * Add the entities, relationships, and similarities of `overlays` (incident
 * graphs, focus neighborhoods) to the pruned radar graph, deduplicated by
 * entity id, source + type + target, and catalog + candidate. Entries already
 * in `base` win. Returns `base` itself when nothing new is added, so memoized
 * consumers keep a stable reference.
 */
export function mergeRadarGraphs(base: RadarGraphResponse, overlays: readonly RadarTopology[]): RadarGraphResponse {
  if (overlays.length === 0) return base
  const entityIds = new Set(base.entities.map((entity) => entity.entity_id))
  const relationshipKeys = new Set(base.relationships.map(
    (edge) => `${edge.source_entity_id}\u0000${edge.relationship_type}\u0000${edge.target_entity_id}`,
  ))
  const similarityKeys = new Set(base.similarities.map(
    (similarity) => `${similarity.catalog_entity_id}\u0000${similarity.candidate_entity_id}`,
  ))
  const entities = [...base.entities]
  const relationships = [...base.relationships]
  const similarities = [...base.similarities]
  for (const overlay of overlays) {
    for (const entity of overlay.entities) {
      if (entityIds.has(entity.entity_id)) continue
      entityIds.add(entity.entity_id)
      entities.push(entity)
    }
    for (const edge of overlay.relationships) {
      const key = `${edge.source_entity_id}\u0000${edge.relationship_type}\u0000${edge.target_entity_id}`
      if (relationshipKeys.has(key)) continue
      relationshipKeys.add(key)
      relationships.push(edge)
    }
    for (const similarity of overlay.similarities) {
      const key = `${similarity.catalog_entity_id}\u0000${similarity.candidate_entity_id}`
      if (similarityKeys.has(key)) continue
      similarityKeys.add(key)
      similarities.push(similarity)
    }
  }
  if (
    entities.length === base.entities.length
    && relationships.length === base.relationships.length
    && similarities.length === base.similarities.length
  ) {
    return base
  }
  return { ...base, entities, relationships, similarities }
}
