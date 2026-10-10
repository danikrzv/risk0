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

import { RadarGraph, visibleRadarEntities } from "./radar-graph"

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
    expect(screen.getByRole("button", { name: /USDC Oracle/ })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Catalog Vault/ })).not.toBeInTheDocument()

    const graphRegion = screen.getByRole("region", { name: "Morpho radar graph" })
    fireEvent.click(screen.getByRole("button", { name: "Expand graph" }))
    expect(graphRegion).toHaveClass("radar-graph--expanded")
    expect(screen.getByRole("button", { name: "Exit expanded graph" })).toHaveAttribute("aria-pressed", "true")
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
    expect(visibleRadarEntities(entities, healthStates, impacts).map((item) => item.entity_id)).toEqual([
      "vault:1:catalog",
      "vault:1:visible",
      "vault:1:unknown-visible",
      "market:1:market",
      "oracle:1:oracle",
      "vault:8453:base",
    ])
  })

  it("keeps unknown affected amounts unavailable instead of displaying zero", () => {
    const unknownImpact = { ...impacts[0], estimated_affected_amount: null }
    render(<RadarGraph graph={graph} healthStates={healthStates} selectedIncident={incident} impacts={[unknownImpact]} />)
    expect(screen.getByLabelText("Incident impact summary")).toHaveTextContent("Affected amount unavailable")
    expect(screen.getByLabelText("Incident impact summary")).not.toHaveTextContent("Affected amount 0")
  })
})
