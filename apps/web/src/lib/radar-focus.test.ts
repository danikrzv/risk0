import type { IncidentImpact, RadarEntity, RadarGraphResponse, RadarRelationship } from "@risk0/contracts"
import { describe, expect, it } from "vitest"

import { rankRadarEntities, relevantDependencyContext } from "./radar-focus"

function entity(entityId: string, label: string, type: RadarEntity["entity_type"] = "vault"): RadarEntity {
  return {
    entity_id: entityId,
    entity_type: type,
    chain_id: 1,
    address_or_key: entityId.split(":").at(-1)!,
    label,
    source: "fixture:morpho",
    catalog: false,
    visible: true,
    metadata: {},
  }
}

function relationship(source: string, target: string, type: RadarRelationship["relationship_type"] = "allocated_to"): RadarRelationship {
  return { source_entity_id: source, target_entity_id: target, relationship_type: type, weight: null, metadata: {} }
}

const entities = [
  entity("vault:1:alpha", "Prime Vault"),
  entity("market:1:prime", "Prime Market", "market"),
  entity("oracle:1:0xfeed", "Feed Oracle", "oracle"),
  entity("vault:1:other", "Other Vault"),
]

describe("rankRadarEntities", () => {
  it("ranks normalized exact matches before prefixes and substrings without disturbing ties", () => {
    const ranked = rankRadarEntities([
      entity("vault:1:contains-prime", "Contains Prime"),
      entity("vault:1:prefix", "Prime Extended"),
      entity("vault:1:prime", "Unrelated"),
      entity("vault:1:label", " PRIME "),
      entity("vault:1:second-prefix", "Prime Secondary"),
    ], "  PrImE ")

    expect(ranked.map((item) => item.entity_id)).toEqual([
      "vault:1:prime",
      "vault:1:label",
      "vault:1:prefix",
      "vault:1:second-prefix",
      "vault:1:contains-prime",
    ])
  })

  it("matches label, ID, address or key, and entity type case-insensitively", () => {
    expect(rankRadarEntities(entities, "feed").map((item) => item.entity_id)).toEqual(["oracle:1:0xfeed"])
    expect(rankRadarEntities(entities, "MARKET:1").map((item) => item.entity_id)).toEqual(["market:1:prime"])
    expect(rankRadarEntities(entities, "0XFEED").map((item) => item.entity_id)).toEqual(["oracle:1:0xfeed"])
    expect(rankRadarEntities(entities, "oracle").map((item) => item.entity_id)).toEqual(["oracle:1:0xfeed"])
  })

  it("returns the original stable order for a blank query", () => {
    expect(rankRadarEntities(entities, "  ")).toEqual(entities)
  })
})

describe("relevantDependencyContext", () => {
  const relationships = [
    relationship("vault:1:alpha", "market:1:prime"),
    relationship("market:1:prime", "oracle:1:0xfeed", "uses_oracle"),
    relationship("vault:1:other", "market:1:other"),
    relationship("vault:1:alpha", "vault:1:similar", "similar_to"),
  ]
  const graph = {
    entities: [
      ...entities,
      entity("market:1:other", "Other Market", "market"),
      entity("vault:1:similar", "Similar Vault"),
      entity("vault:1:shared", "Shared Dependency Vault"),
      entity("position:1:manual", "Manual Position", "position"),
    ],
    relationships: [
      ...relationships,
      relationship("vault:1:alpha", "vault:1:shared", "similar_to"),
      relationship("vault:1:shared", "market:1:prime"),
      relationship("position:1:manual", "vault:1:alpha", "lends"),
    ],
  } satisfies Pick<RadarGraphResponse, "entities" | "relationships">

  it("walks dependencies outward, includes an attached manual position, and excludes unrelated dependents and similarity", () => {
    expect(relevantDependencyContext(graph, "vault:1:alpha", [])).toEqual(new Set([
      "vault:1:alpha",
      "position:1:manual",
      "market:1:prime",
      "oracle:1:0xfeed",
      "vault:1:shared",
    ]))
    expect(relevantDependencyContext(graph, "market:1:prime", [])).toEqual(new Set([
      "market:1:prime",
      "oracle:1:0xfeed",
    ]))
  })

  it("includes the complete incident path affecting the selected entity", () => {
    const impact: IncidentImpact = {
      incident_id: "incident:oracle",
      target_entity_id: "vault:1:alpha",
      path: ["oracle:1:incident", "market:1:incident", "vault:1:alpha"],
      distance: 2,
      confidence: "0.9",
      estimated_affected_amount: null,
      reasons: [],
      revealed: true,
      alternate_path_count: 0,
    }

    expect(relevantDependencyContext(graph, "vault:1:alpha", [impact])).toEqual(new Set([
      "vault:1:alpha",
      "market:1:prime",
      "position:1:manual",
      "oracle:1:0xfeed",
      "oracle:1:incident",
      "market:1:incident",
      "vault:1:shared",
    ]))
  })

  it("stays bounded across duplicates, cycles, self-loops, and unknown endpoints", () => {
    const cyclic = {
      entities: [entity("a", "A"), entity("b", "B"), entity("c", "C")],
      relationships: [
        relationship("a", "b"),
        relationship("a", "b"),
        relationship("b", "c"),
        relationship("c", "a"),
        relationship("a", "a"),
        relationship("c", "missing"),
        relationship("unknown", "a"),
      ],
    }

    expect([...relevantDependencyContext(cyclic, "a", [])]).toEqual(["a", "b", "c"])
    expect(relevantDependencyContext(cyclic, "missing", [])).toEqual(new Set())
  })
})
