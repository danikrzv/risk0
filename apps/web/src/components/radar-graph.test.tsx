import "@testing-library/jest-dom/vitest"

import type {
  HealthState,
  Incident,
  IncidentImpact,
  RadarEntity,
  RadarGraphResponse,
  RadarRelationship,
} from "@risk0/contracts"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { layeredLayout } from "../lib/layered-layout"
import { MAX_CANVAS_NODES, RadarGraph, visibleRadarEntities } from "./radar-graph"

vi.mock("../lib/layered-layout", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/layered-layout")>()
  return { ...original, layeredLayout: vi.fn(original.layeredLayout) }
})

afterEach(cleanup)

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

function entity(
  entityId: string,
  label: string,
  options: Partial<Pick<RadarEntity, "entity_type" | "catalog" | "visible" | "chain_id">> = {},
): RadarEntity {
  return {
    entity_id: entityId,
    entity_type: options.entity_type ?? "vault",
    chain_id: options.chain_id ?? 1,
    address_or_key: entityId.split(":").at(-1)!,
    label,
    source: "fixture:morpho",
    catalog: options.catalog ?? false,
    visible: options.visible ?? false,
    metadata: {},
  }
}

const entities: RadarEntity[] = [
  entity("vault:1:catalog", "Catalog Vault", { catalog: true, visible: true }),
  entity("vault:1:visible", "Visible Neighbor", { visible: true }),
  entity("vault:1:healthy-hidden", "Healthy Hidden Neighbor"),
  entity("vault:1:unknown-visible", "Unknown Visible Neighbor", { visible: true }),
  entity("vault:1:warning-hidden", "Warning Hidden Neighbor"),
  entity("market:1:market", "USDC Market", { entity_type: "market", visible: true }),
  entity("oracle:1:oracle", "USDC Oracle", { entity_type: "oracle" }),
  entity("vault:8453:base", "Base Vault", { visible: true, chain_id: 8453 }),
]

const relationships: RadarRelationship[] = [
  {
    source_entity_id: "vault:1:catalog",
    target_entity_id: "market:1:market",
    relationship_type: "allocated_to",
    weight: "0.60",
    metadata: {},
  },
  {
    source_entity_id: "market:1:market",
    target_entity_id: "oracle:1:oracle",
    relationship_type: "uses_oracle",
    weight: null,
    metadata: {},
  },
]

const graph: RadarGraphResponse = {
  schema_version: "1.0.0",
  entities,
  relationships,
  similarities: [{
    catalog_entity_id: "vault:1:catalog",
    candidate_entity_id: "vault:1:visible",
    score: "0.72",
    components: { shared_markets: "0.35", asset_overlap: "0.20" },
    missing_dimensions: ["oracle_overlap"],
    model_version: "vault-similarity-v1",
    stored: true,
    normally_visible: true,
    computed_at: "2026-10-10T12:05:00Z",
  }],
  active_incidents: [],
  generated_at: "2026-10-10T12:05:00Z",
}

const healthStates: HealthState[] = [
  {
    entity_id: "vault:1:catalog",
    status: "HEALTHY",
    score: "0.99",
    reasons: [],
    missing_fields: [],
    evaluated_at: "2026-10-10T12:05:00Z",
    rule_version: "vault-health-v1",
  },
  {
    entity_id: "vault:1:visible",
    status: "WARNING",
    score: "0.60",
    reasons: ["Liquidity is falling."],
    missing_fields: [],
    evaluated_at: "2026-10-10T12:05:00Z",
    rule_version: "vault-health-v1",
  },
  {
    entity_id: "vault:1:healthy-hidden",
    status: "HEALTHY",
    score: "0.95",
    reasons: [],
    missing_fields: [],
    evaluated_at: "2026-10-10T12:05:00Z",
    rule_version: "vault-health-v1",
  },
  {
    entity_id: "vault:1:warning-hidden",
    status: "WARNING",
    score: "0.60",
    reasons: ["Unrelated hidden warning."],
    missing_fields: [],
    evaluated_at: "2026-10-10T12:05:00Z",
    rule_version: "vault-health-v1",
  },
]

const incident: Incident = {
  incident_id: "incident:oracle",
  source_entity_id: "oracle:1:oracle",
  severity: "CRITICAL",
  status: "OPEN",
  trigger_metrics: { deviation: "0.08" },
  rule_version: "oracle-health-v1",
  first_observed_at: "2026-10-10T12:05:00Z",
  last_observed_at: "2026-10-10T12:05:00Z",
  resolved_at: null,
  reasons: ["Oracle deviation is critical."],
}

const impacts: IncidentImpact[] = [
  {
    incident_id: "incident:oracle",
    target_entity_id: "vault:1:catalog",
    path: ["oracle:1:oracle", "market:1:market", "vault:1:catalog"],
    distance: 2,
    confidence: "0.95",
    estimated_affected_amount: "60000",
    reasons: ["The vault allocates to the affected market."],
    revealed: true,
    alternate_path_count: 0,
  },
]

describe("RadarGraph", () => {
  it("shows catalog vaults and visible neighbors while hiding healthy invisible secondary vaults", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} />)

    const nodes = screen.getByRole("list", { name: "Radar nodes" })
    expect(within(nodes).getByRole("button", { name: /Catalog Vault/ })).toBeInTheDocument()
    expect(within(nodes).getByRole("button", { name: /^Visible Neighbor/ })).toBeInTheDocument()
    expect(within(nodes).queryByRole("button", { name: /Healthy Hidden Neighbor/ })).not.toBeInTheDocument()
  })

  it("treats UNKNOWN as a distinct non-healthy state and exposes all four legend statuses", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} />)

    const unknown = screen.getByRole("button", { name: /Unknown Visible Neighbor.*UNKNOWN/ })
    expect(unknown).toHaveAttribute("data-health-status", "UNKNOWN")
    expect(unknown).not.toHaveClass("radar-node--healthy")

    const legend = screen.getByRole("list", { name: "Health status legend" })
    for (const status of ["HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"]) {
      expect(within(legend).getByText(status)).toBeInTheDocument()
    }
  })

  it("reveals impacted hidden nodes and highlights the selected incident path", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} selectedIncident={incident} impacts={impacts} />)

    const oracle = screen.getByRole("button", { name: /USDC Oracle.*CRITICAL/ })
    expect(oracle).toHaveAttribute("data-incident-path", "true")
    expect(oracle).toHaveAttribute("data-revealed", "true")

    const relationshipsList = screen.getByRole("list", { name: "Radar relationships" })
    expect(within(relationshipsList).getByText(/USDC Market.*USDC Oracle/).closest("li")).toHaveAttribute(
      "data-incident-path",
      "true",
    )
  })

  it("highlights only edges that lie on a propagation path, not every edge between path nodes", () => {
    const shortcut: RadarRelationship = {
      ...relationships[0],
      source_entity_id: "vault:1:catalog",
      target_entity_id: "oracle:1:oracle",
      relationship_type: "similar_to",
    }
    render(
      <RadarGraph
        graph={{ ...graph, relationships: [...relationships, shortcut] }}
        healthStates={healthStates}
        selectedIncident={incident}
        impacts={impacts}
      />,
    )

    const list = screen.getByRole("list", { name: "Radar relationships" })
    expect(within(list).getByText(/similar to/).closest("li")).toHaveAttribute("data-incident-path", "false")
    expect(within(list).getByText(/allocated to/).closest("li")).toHaveAttribute("data-incident-path", "true")
  })

  it("selects graph entities through accessible node controls", () => {
    const onSelectEntity = vi.fn()
    render(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} />)

    fireEvent.click(screen.getByRole("button", { name: /^Visible Neighbor/ }))
    expect(onSelectEntity).toHaveBeenCalledWith("vault:1:visible")
  })

  it("inspects the selected entity, navigates its connections, and offers analysis only for catalog matches", () => {
    const onSelectEntity = vi.fn()
    const onAnalyzeEntity = vi.fn()
    const { rerender } = render(
      <RadarGraph
        graph={graph}
        healthStates={healthStates}
        selectedEntityId="vault:1:catalog"
        onSelectEntity={onSelectEntity}
        analyzableEntityIds={new Set(["vault:1:catalog"])}
        onAnalyzeEntity={onAnalyzeEntity}
      />,
    )

    const detail = screen.getByRole("region", { name: "Selected radar entity" })
    expect(within(detail).getByRole("heading", { name: "Catalog Vault" })).toBeInTheDocument()
    fireEvent.click(within(detail).getByRole("button", { name: "USDC Market" }))
    expect(onSelectEntity).toHaveBeenCalledWith("market:1:market")
    fireEvent.click(within(detail).getByRole("button", { name: "Analyze a position here" }))
    expect(onAnalyzeEntity).toHaveBeenCalledWith("vault:1:catalog")
    fireEvent.click(within(detail).getByRole("button", { name: "Close entity details" }))
    expect(onSelectEntity).toHaveBeenLastCalledWith(null)

    rerender(
      <RadarGraph
        graph={graph}
        healthStates={healthStates}
        selectedEntityId="market:1:market"
        onSelectEntity={onSelectEntity}
        analyzableEntityIds={new Set(["vault:1:catalog"])}
        onAnalyzeEntity={onAnalyzeEntity}
      />,
    )
    expect(screen.queryByRole("button", { name: "Analyze a position here" })).not.toBeInTheDocument()
  })

  it("lays the graph out once and keeps it across selection changes", () => {
    const onSelectEntity = vi.fn()
    vi.mocked(layeredLayout).mockClear()
    const { rerender } = render(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} />)
    expect(layeredLayout).toHaveBeenCalledTimes(1)

    rerender(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} selectedEntityId="vault:1:catalog" />)
    rerender(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} selectedEntityId="market:1:market" />)
    expect(screen.getByRole("button", { name: /^USDC Market/ })).toHaveAttribute("aria-pressed", "true")
    expect(layeredLayout).toHaveBeenCalledTimes(1)
  })

  it("filters visible nodes by chain, entity type, and health", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} />)

    fireEvent.change(screen.getByLabelText("Chain filter"), { target: { value: "8453" } })
    expect(screen.getByRole("button", { name: /Base Vault/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Catalog Vault/ })).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText("Chain filter"), { target: { value: "all" } })
    fireEvent.change(screen.getByLabelText("Entity type filter"), { target: { value: "oracle" } })
    expect(screen.getByRole("button", { name: /USDC Oracle/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Catalog Vault/ })).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText("Entity type filter"), { target: { value: "all" } })
    fireEvent.change(screen.getByLabelText("Health filter"), { target: { value: "WARNING" } })
    expect(screen.getByRole("button", { name: /^Visible Neighbor/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Warning Hidden Neighbor/ })).not.toBeInTheDocument()
  })

  it("searches entities and expands the graph workspace", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} />)

    fireEvent.change(screen.getByLabelText("Search graph"), { target: { value: "oracle" } })
    const results = screen.getByRole("list", { name: "Graph search results" })
    expect(within(results).getByRole("button", { name: /USDC Oracle/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Catalog Vault/ })).toBeInTheDocument()

    const graphRegion = screen.getByRole("region", { name: "Morpho radar graph" })
    fireEvent.click(screen.getByRole("button", { name: "Expand graph" }))
    expect(graphRegion).toHaveClass("radar-graph--expanded")
    expect(screen.getByRole("button", { name: "Exit expanded graph" })).toHaveAttribute("aria-pressed", "true")
  })

  it.each([
    ["chain", "Chain filter", "8453"],
    ["entity type", "Entity type filter", "oracle"],
    ["health", "Health filter", "WARNING"],
  ])("clears a conflicting %s filter when a search result is selected", (_, filterLabel, filterValue) => {
    const onSelectEntity = vi.fn()
    const { rerender } = render(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} />)

    fireEvent.change(screen.getByLabelText(filterLabel), { target: { value: filterValue } })
    fireEvent.change(screen.getByLabelText("Search graph"), { target: { value: "catalog vault" } })
    fireEvent.click(within(screen.getByRole("list", { name: "Graph search results" })).getByRole("button", { name: /Catalog Vault/ }))

    expect(screen.getByLabelText(filterLabel)).toHaveValue("all")
    expect(onSelectEntity).toHaveBeenCalledWith("vault:1:catalog")
    rerender(<RadarGraph graph={graph} healthStates={healthStates} selectedEntityId="vault:1:catalog" onSelectEntity={onSelectEntity} />)
    expect(within(screen.getByRole("list", { name: "Radar nodes" })).getByRole("button", { name: /Catalog Vault/ })).toBeInTheDocument()
  })

  it("draws an entity selected from outside the graph even when the graph's filters hide it", () => {
    const onSelectEntity = vi.fn()
    const { rerender } = render(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} />)
    fireEvent.change(screen.getByLabelText("Entity type filter"), { target: { value: "oracle" } })
    const nodes = () => within(screen.getByRole("list", { name: "Radar nodes" }))
    expect(nodes().queryByRole("button", { name: /Catalog Vault/ })).not.toBeInTheDocument()

    // As when a Markets row or View dependencies selects it.
    rerender(<RadarGraph graph={graph} healthStates={healthStates} selectedEntityId="vault:1:catalog" onSelectEntity={onSelectEntity} />)
    expect(nodes().getByRole("button", { name: /Catalog Vault/ })).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByLabelText("Entity type filter")).toHaveValue("oracle")
  })

  it("searches loaded entities the policy does not show, but never unrevealed hidden vaults", () => {
    const orphan = entity("market:1:orphan", "Orphan Market", { entity_type: "market" })
    render(<RadarGraph graph={{ ...graph, entities: [...graph.entities, orphan] }} healthStates={healthStates} onSelectEntity={vi.fn()} />)
    const search = screen.getByLabelText("Search graph")

    fireEvent.change(search, { target: { value: "orphan" } })
    expect(within(screen.getByRole("list", { name: "Graph search results" })).getByRole("button", { name: /Orphan Market/ })).toBeInTheDocument()

    fireEvent.change(search, { target: { value: "Healthy Hidden Neighbor" } })
    expect(screen.queryByRole("list", { name: "Graph search results" })).not.toBeInTheDocument()
    expect(screen.getByText("No graph entities match.")).toBeInTheDocument()
  })

  it("closing the entity details also leaves relevant-only mode", () => {
    const onSelectEntity = vi.fn()
    render(<RadarGraph graph={graph} healthStates={healthStates} selectedEntityId="vault:1:catalog" onSelectEntity={onSelectEntity} />)
    const relevantOnly = screen.getByRole("button", { name: "Show only relevant" })
    fireEvent.click(relevantOnly)
    expect(relevantOnly).toHaveAttribute("aria-pressed", "true")

    fireEvent.click(screen.getByRole("button", { name: "Close entity details" }))
    expect(onSelectEntity).toHaveBeenLastCalledWith(null)
    expect(relevantOnly).toHaveAttribute("aria-pressed", "false")
  })

  it("promotes a selected search result beyond the canvas cap", () => {
    const many = Array.from({ length: MAX_CANVAS_NODES + 1 }, (_, index) => (
      entity(`vault:1:many-${index}`, index === MAX_CANVAS_NODES ? "Needle Vault" : `Many Vault ${index}`, { visible: true })
    ))
    const cappedGraph = { ...graph, entities: many, relationships: [], similarities: [] }
    const onSelectEntity = vi.fn()
    vi.mocked(layeredLayout).mockClear()
    const { rerender } = render(<RadarGraph graph={cappedGraph} onSelectEntity={onSelectEntity} />)

    fireEvent.change(screen.getByLabelText("Health filter"), { target: { value: "HEALTHY" } })
    fireEvent.change(screen.getByLabelText("Search graph"), { target: { value: "needle" } })
    fireEvent.click(within(screen.getByRole("list", { name: "Graph search results" })).getByRole("button", { name: /Needle Vault/ }))
    expect(screen.getByLabelText("Health filter")).toHaveValue("all")
    expect(onSelectEntity).toHaveBeenCalledWith("vault:1:many-400")

    rerender(<RadarGraph graph={cappedGraph} selectedEntityId="vault:1:many-400" onSelectEntity={onSelectEntity} />)
    const laidOutIds = vi.mocked(layeredLayout).mock.calls.at(-1)?.[0] ?? []
    expect(laidOutIds).toHaveLength(MAX_CANVAS_NODES)
    expect(laidOutIds).toContain("vault:1:many-400")
  })

  it("fades unrelated topology, can hide it, and clear focus restores the complete graph", () => {
    const onSelectEntity = vi.fn()
    const { container, rerender } = render(
      <RadarGraph graph={graph} healthStates={healthStates} selectedEntityId="vault:1:catalog" onSelectEntity={onSelectEntity} />,
    )

    expect(container.querySelectorAll(".radar-flow-node--faded").length).toBeGreaterThan(0)
    expect(within(screen.getByRole("list", { name: "Radar relationships" })).getAllByRole("listitem")[0]).toHaveAttribute("data-focus", "focused")

    fireEvent.click(screen.getByRole("button", { name: "Show only relevant" }))
    expect(screen.queryByRole("button", { name: /Base Vault/ })).not.toBeInTheDocument()
    expect(within(screen.getByRole("list", { name: "Radar relationships" })).getAllByRole("listitem")).toHaveLength(2)

    fireEvent.click(screen.getByRole("button", { name: "Clear focus" }))
    expect(onSelectEntity).toHaveBeenCalledWith(null)
    rerender(<RadarGraph graph={graph} healthStates={healthStates} onSelectEntity={onSelectEntity} />)
    expect(screen.getByRole("button", { name: /Base Vault/ })).toBeInTheDocument()
    expect(container.querySelector(".radar-flow-node--faded")).not.toBeInTheDocument()
  })

  it("shows relationship labels only for focused, incident, or explicit label context", () => {
    const { rerender } = render(<RadarGraph graph={graph} healthStates={healthStates} />)
    const allocatedTo = () => within(screen.getByRole("list", { name: "Radar relationships" })).getByText(/allocated to/)
    const usesOracle = () => within(screen.getByRole("list", { name: "Radar relationships" })).getByText(/uses oracle/)
    expect(allocatedTo()).toHaveAttribute("data-label-visible", "false")

    rerender(<RadarGraph graph={graph} healthStates={healthStates} selectedEntityId="vault:1:catalog" />)
    expect(allocatedTo()).toHaveAttribute("data-label-visible", "true")

    rerender(<RadarGraph graph={graph} healthStates={healthStates} selectedIncident={incident} impacts={impacts} />)
    expect(usesOracle()).toHaveAttribute("data-label-visible", "true")

    rerender(<RadarGraph graph={graph} healthStates={healthStates} />)
    fireEvent.click(screen.getByRole("button", { name: "Show relationship labels" }))
    expect(allocatedTo()).toHaveAttribute("data-label-visible", "true")
  })

  it("explains similarity scores for visible neighboring vaults", () => {
    render(<RadarGraph graph={graph} healthStates={healthStates} />)

    const explanations = screen.getByRole("list", { name: "Similarity explanations" })
    expect(explanations).toHaveTextContent("Visible Neighbor")
    expect(explanations).toHaveTextContent("72%")
    expect(explanations).toHaveTextContent("shared markets 35%")
    expect(explanations).toHaveTextContent("Missing: oracle overlap")
  })

  it("derives visibility deterministically from health and incident impact", () => {
    expect(visibleRadarEntities({ entities, relationships }, impacts).map((item) => item.entity_id)).toEqual([
      "vault:1:catalog",
      "vault:1:visible",
      "vault:1:unknown-visible",
      "market:1:market",
      "oracle:1:oracle",
      "vault:8453:base",
    ])
  })

  it("shows non-vault entities only when connected to a shown vault or catalog entry", () => {
    const orphanEntities = [
      ...entities,
      entity("market:1:hidden-only", "Hidden Vault Market", { entity_type: "market", visible: true }),
      entity("oracle:1:hidden-only", "Hidden Vault Oracle", { entity_type: "oracle" }),
      entity("asset:1:orphan", "Orphan Asset", { entity_type: "asset", visible: true }),
      entity("position:1:catalog", "Catalog Position", { entity_type: "position" }),
    ]
    const orphanRelationships: RadarRelationship[] = [
      ...relationships,
      { ...relationships[0], source_entity_id: "vault:1:healthy-hidden", target_entity_id: "market:1:hidden-only" },
      { ...relationships[1], source_entity_id: "market:1:hidden-only", target_entity_id: "oracle:1:hidden-only" },
      { ...relationships[1], source_entity_id: "position:1:catalog", target_entity_id: "vault:1:catalog", relationship_type: "lends" },
    ]

    const ids = visibleRadarEntities({ entities: orphanEntities, relationships: orphanRelationships }).map((item) => item.entity_id)
    // The catalog vault's market, its oracle (a dependency of that market), and a direct dependent are shown.
    expect(ids).toEqual(expect.arrayContaining(["market:1:market", "oracle:1:oracle", "position:1:catalog"]))
    // A hidden vault's dependencies and unconnected entities are not, even when flagged visible.
    expect(ids).not.toContain("market:1:hidden-only")
    expect(ids).not.toContain("oracle:1:hidden-only")
    expect(ids).not.toContain("asset:1:orphan")
    expect(ids).not.toContain("vault:1:healthy-hidden")

    // An incident path reveals them.
    const hiddenImpact: IncidentImpact = {
      ...impacts[0],
      target_entity_id: "vault:1:healthy-hidden",
      path: ["oracle:1:hidden-only", "market:1:hidden-only", "vault:1:healthy-hidden"],
    }
    expect(
      visibleRadarEntities({ entities: orphanEntities, relationships: orphanRelationships }, [hiddenImpact]).map((item) => item.entity_id),
    ).toEqual(expect.arrayContaining(["oracle:1:hidden-only", "market:1:hidden-only", "vault:1:healthy-hidden"]))
  })

  it("keeps the visible set and the rendered output bounded on a radar-sized graph", () => {
    // ~20k entities / ~36k relationships: 10 shown vaults among 6,000 hidden ones,
    // each vault on 3 of 6,000 markets that share 2,000 assets and 6,000 oracles.
    const big: RadarEntity[] = []
    const edges: RadarRelationship[] = []
    const link = (source: string, target: string, type: RadarRelationship["relationship_type"]): void => {
      edges.push({ source_entity_id: source, target_entity_id: target, relationship_type: type, weight: null, metadata: {} })
    }
    for (let index = 0; index < 2_000; index += 1) big.push(entity(`asset:1:${index}`, `Asset ${index}`, { entity_type: "asset", visible: true }))
    for (let index = 0; index < 6_000; index += 1) {
      big.push(entity(`market:1:${index}`, `Market ${index}`, { entity_type: "market", visible: true }))
      big.push(entity(`oracle:1:${index}`, `Oracle ${index}`, { entity_type: "oracle", visible: true }))
      link(`market:1:${index}`, `oracle:1:${index}`, "uses_oracle")
      link(`market:1:${index}`, `asset:1:${index % 2_000}`, "lends")
      link(`market:1:${index}`, `asset:1:${(index + 1) % 2_000}`, "uses_collateral")
    }
    for (let index = 0; index < 6_010; index += 1) {
      const shown = index < 10
      big.push(entity(`vault:1:${index}`, `Vault ${index}`, { catalog: index === 0, visible: shown }))
      for (let offset = 0; offset < 3; offset += 1) link(`vault:1:${index}`, `market:1:${(index * 3 + offset) % 6_000}`, "allocated_to")
    }
    expect(big.length).toBeGreaterThan(20_000)
    expect(edges.length).toBeGreaterThan(35_000)

    const started = performance.now()
    const visible = visibleRadarEntities({ entities: big, relationships: edges })
    expect(performance.now() - started).toBeLessThan(1_000)
    // 10 vaults + 30 markets + 30 oracles + 31 distinct assets.
    expect(visible).toHaveLength(101)
    expect(visible.every((item) => item.entity_type !== "vault" || item.visible)).toBe(true)

    render(<RadarGraph graph={{ ...graph, entities: big, relationships: edges, similarities: [] }} />)
    const nodes = screen.getByRole("list", { name: "Radar nodes" })
    expect(within(nodes).getAllByRole("button").length).toBeLessThanOrEqual(60)
    expect(screen.getByText("Showing 60 of 101 nodes")).toBeInTheDocument()
    expect(within(screen.getByRole("list", { name: "Radar relationships" })).getAllByRole("listitem").length).toBeLessThanOrEqual(60)
  })

  it("caps the accessible node list and reveals more on request", () => {
    const many = Array.from({ length: 130 }, (_, index) => entity(`vault:1:many-${index}`, `Many Vault ${index}`, { visible: true }))
    render(<RadarGraph graph={{ ...graph, entities: [...entities, ...many] }} healthStates={healthStates} />)

    const nodes = screen.getByRole("list", { name: "Radar nodes" })
    expect(within(nodes).getAllByRole("button")).toHaveLength(60)
    // The catalog entry is listed first even though the list is capped.
    expect(within(nodes).getAllByRole("button")[0]).toHaveAccessibleName(/Catalog Vault/)
    const more = screen.getByRole("button", { name: "Show 60 more nodes" })
    expect(more).toHaveAttribute("aria-controls", nodes.id)

    fireEvent.click(more)
    expect(within(nodes).getAllByRole("button")).toHaveLength(120)
    fireEvent.click(screen.getByRole("button", { name: "Show 16 more nodes" }))
    expect(within(nodes).getAllByRole("button")).toHaveLength(136)
    expect(screen.queryByRole("button", { name: /more nodes/ })).not.toBeInTheDocument()
  })

  it("caps the canvas and says so when too many entities match", () => {
    const many = Array.from({ length: MAX_CANVAS_NODES + 5 }, (_, index) => entity(`vault:1:many-${index}`, `Many Vault ${index}`, { visible: true }))
    render(<RadarGraph graph={{ ...graph, entities: many, relationships: [], similarities: [] }} />)

    expect(screen.getByText(new RegExp(`first ${MAX_CANVAS_NODES} of ${MAX_CANVAS_NODES + 5} entities`))).toBeInTheDocument()
  })

  it("explains an empty radar instead of drawing a blank canvas", () => {
    render(<RadarGraph graph={{ ...graph, entities: [], relationships: [], similarities: [] }} />)

    expect(screen.getByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.getByText("Radar has no data yet — discovery runs hourly")).toBeInTheDocument()
    expect(screen.queryByLabelText("Chain filter")).not.toBeInTheDocument()
  })

  it("keeps unknown affected amounts unavailable instead of displaying zero", () => {
    const unknownImpact = { ...impacts[0], estimated_affected_amount: null }
    render(<RadarGraph graph={graph} healthStates={healthStates} selectedIncident={incident} impacts={[unknownImpact]} />)
    expect(screen.getByLabelText("Incident impact summary")).toHaveTextContent("Affected amount unavailable")
    expect(screen.getByLabelText("Incident impact summary")).not.toHaveTextContent("Affected amount 0")
  })
})
