import type { RadarEntity, RadarGraphResponse, RadarRelationship, SimilarityRecord } from "@risk0/contracts"
import { describe, expect, it } from "vitest"

import { mergeRadarGraphs } from "./radar-graph-merge"

function entity(entityId: string, label = entityId): RadarEntity {
  return {
    entity_id: entityId, entity_type: "market", chain_id: 1, address_or_key: entityId, label,
    source: "fixture", catalog: false, visible: true, metadata: {},
  }
}

function edge(source: string, target: string, type: RadarRelationship["relationship_type"] = "uses_oracle"): RadarRelationship {
  return { source_entity_id: source, target_entity_id: target, relationship_type: type, weight: null, metadata: {} }
}

function similarity(catalog: string, candidate: string): SimilarityRecord {
  return {
    catalog_entity_id: catalog, candidate_entity_id: candidate, score: "0.5", components: {}, missing_dimensions: [],
    model_version: "vault-similarity-v1", stored: true, normally_visible: false, computed_at: "2026-10-10T12:00:00Z",
  }
}

const base: RadarGraphResponse = {
  schema_version: "1.0.0",
  entities: [entity("a", "Base A"), entity("b")],
  relationships: [edge("a", "b")],
  similarities: [similarity("a", "b")],
  active_incidents: [],
  generated_at: "2026-10-10T12:00:00Z",
}

describe("mergeRadarGraphs", () => {
  it("adds missing entities, relationships, and similarities without duplicates", () => {
    const merged = mergeRadarGraphs(base, [
      { entities: [entity("a", "Overlay A"), entity("c")], relationships: [edge("a", "b"), edge("b", "c"), edge("a", "b", "lends")], similarities: [similarity("a", "b"), similarity("a", "c")] },
      { entities: [entity("c"), entity("d")], relationships: [edge("b", "c")], similarities: [] },
    ])

    expect(merged.entities.map((item) => item.entity_id)).toEqual(["a", "b", "c", "d"])
    // The base entry wins over an overlay copy.
    expect(merged.entities[0].label).toBe("Base A")
    expect(merged.relationships).toHaveLength(3)
    expect(merged.similarities).toHaveLength(2)
    expect(merged.active_incidents).toBe(base.active_incidents)
  })

  it("returns the base graph itself when overlays add nothing", () => {
    expect(mergeRadarGraphs(base, [])).toBe(base)
    expect(mergeRadarGraphs(base, [{ entities: [entity("a")], relationships: [edge("a", "b")], similarities: [] }])).toBe(base)
  })
})
