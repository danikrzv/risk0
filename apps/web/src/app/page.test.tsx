import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"

import { analysisResponse, catalogResponse } from "../test-fixtures"
import Home from "./page"

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub)
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal("EventSource", class { addEventListener() {} close() {} })
})

const radarGraph = {
  schema_version: "1.0.0",
  entities: [
    { entity_id: "vault:1:catalog", entity_type: "vault", chain_id: 1, address_or_key: "catalog", label: "Belem Sentinel USDC", source: "fixture:radar", catalog: true, visible: true, metadata: { health_status: "HEALTHY" } },
    { entity_id: "vault:1:peer", entity_type: "vault", chain_id: 1, address_or_key: "peer", label: "Institutional USDC Peer", source: "fixture:radar", catalog: false, visible: true, metadata: { health_status: "HEALTHY" } },
  ],
  relationships: [], similarities: [], active_incidents: [], generated_at: "2026-10-10T12:00:00Z",
}

const categoryRadarGraph = {
  ...radarGraph,
  entities: [
    ...radarGraph.entities,
    { entity_id: "market:1:healthy", entity_type: "market", chain_id: 1, address_or_key: "healthy-market", label: "Healthy Market", source: "fixture:radar", catalog: false, visible: true, metadata: { health_status: "HEALTHY" } },
    { entity_id: "market:1:unknown", entity_type: "market", chain_id: 1, address_or_key: "unknown-market", label: "Unknown Market", source: "fixture:radar", catalog: false, visible: true, metadata: { health_status: "BROKEN" } },
    { entity_id: "oracle:1:warning", entity_type: "oracle", chain_id: 1, address_or_key: "warning-oracle", label: "Warning Oracle", source: "fixture:radar", catalog: false, visible: true, metadata: { health_status: "WARNING" } },
  ],
  active_incidents: [{
    incident_id: "incident:market", source_entity_id: "market:1:healthy", severity: "CRITICAL", status: "OPEN",
    trigger_metrics: {}, rule_version: "v1", first_observed_at: "2026-10-10T12:00:00Z",
    last_observed_at: "2026-10-10T12:00:00Z", resolved_at: null, reasons: ["Liquidity threshold breached."],
  }],
}

function stubApi(analysis: Response | (() => Promise<Response>) = Response.json(analysisResponse)) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/catalog") return Response.json(catalogResponse)
    if (url === "/api/graph") return Response.json(radarGraph)
    if (url.startsWith("/api/notifications")) return Response.json({ schema_version: "1.0.0", notifications: [], next_cursor: null, generated_at: "2026-10-10T12:00:00Z" })
    if (url === "/api/exposures") return Response.json({ exposure_id: "exposure:test", entity_id: "vault:1:catalog", mode: "lender", amount: "100000", numeraire: "USDC", collateral_amount: null, debt_amount: null, ltv: null, created_at: "2026-10-10T12:01:00Z" }, { status: 201 })
    if (url === "/api/analysis") return typeof analysis === "function" ? analysis() : analysis
    throw new Error(`unexpected ${url}`)
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

/** The incident drawer starts closed: it shares the canvas edge with the exposure panel. */
async function openIncidentDrawer() {
  fireEvent.click(await screen.findByRole("button", { name: /^Incidents \(\d+\)$/ }))
}

function openPositionAnalysis() {
  fireEvent.click(screen.getByRole("button", { name: "Position Analysis" }))
}

describe("Home", () => {
  it("renders the Risk0 heading", () => {
    stubApi()
    render(<Home />)
    expect(screen.getByRole("heading", { name: "Risk0" })).toBeInTheDocument()
  })

  it("opens on the full-screen Graph category without the removed masthead", async () => {
    stubApi()
    render(<Home />)

    expect(screen.getByRole("button", { name: "Graph" })).toHaveAttribute("aria-current", "page")
    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.queryByText("Monitor dependencies, propagation, and portfolio impact")).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Position analysis" })).not.toBeInTheDocument()
  })

  it("switches between health-marked Markets, Oracles, Incidents, and Position Analysis", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json(categoryRadarGraph) : base(url)
    )))
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })

    fireEvent.click(screen.getByRole("button", { name: "Markets" }))
    const markets = screen.getByRole("region", { name: "Markets" })
    expect(within(markets).getByRole("button", { name: /Healthy Market.*HEALTHY/ })).toHaveTextContent("✓ HEALTHY")
    expect(within(markets).getByRole("button", { name: /Unknown Market.*UNKNOWN/ })).toHaveTextContent("? UNKNOWN")

    fireEvent.click(screen.getByRole("button", { name: "Oracles" }))
    const oracles = screen.getByRole("region", { name: "Oracles" })
    expect(within(oracles).getByRole("button", { name: /Warning Oracle.*WARNING/ })).toHaveTextContent("! WARNING")

    fireEvent.click(screen.getByRole("button", { name: "Incidents" }))
    const incidents = screen.getByRole("region", { name: "Incidents" })
    expect(within(incidents).getByRole("button", { name: /Healthy Market.*CRITICAL/ })).toHaveTextContent("× CRITICAL")

    fireEvent.click(screen.getByRole("button", { name: "Position Analysis" }))
    expect(screen.getByRole("heading", { name: "Position analysis" })).toBeInTheDocument()
    expect(await screen.findByRole("button", { name: /Belem RWA EURCV.*UNKNOWN/ })).toHaveTextContent("? UNKNOWN")
  })

  it("explains empty market, oracle, and incident lists", async () => {
    stubApi()
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })

    fireEvent.click(screen.getByRole("button", { name: "Markets" }))
    expect(screen.getByText("No markets are currently available.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Oracles" }))
    expect(screen.getByText("No oracles are currently available.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Incidents" }))
    expect(within(screen.getByRole("region", { name: "Incidents" })).getByText("No active incidents.")).toBeInTheDocument()
  })

  it("shows radar loading state instead of empty category messages while the graph is pending", () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/graph") return new Promise<Response>(() => {})
      return base(url)
    }))
    render(<Home />)

    for (const category of ["Markets", "Oracles", "Incidents"]) {
      fireEvent.click(screen.getByRole("button", { name: category }))
      const region = screen.getByRole("region", { name: category })
      expect(within(region).getByText("Loading radar topology…")).toHaveAttribute("role", "status")
      expect(within(region).queryByText(/No (markets|oracles|active incidents)/)).not.toBeInTheDocument()
    }
  })

  it("shows radar errors instead of empty category messages when the graph fails", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json({ error: "Risk service unavailable" }, { status: 502 }) : base(url)
    )))
    render(<Home />)
    await screen.findByText("DEGRADED")

    for (const category of ["Markets", "Oracles", "Incidents"]) {
      fireEvent.click(screen.getByRole("button", { name: category }))
      const region = screen.getByRole("region", { name: category })
      expect(within(region).getByRole("alert")).toHaveTextContent("Risk service unavailable")
      expect(within(region).queryByText(/No (markets|oracles|active incidents)/)).not.toBeInTheDocument()
    }
  })

  it("returns to Graph and selects the entity chosen from a category list", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json(categoryRadarGraph) : base(url)
    )))
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })

    fireEvent.click(screen.getByRole("button", { name: "Markets" }))
    fireEvent.click(screen.getByRole("button", { name: /Healthy Market.*HEALTHY/ }))

    expect(screen.getByRole("button", { name: "Graph" })).toHaveAttribute("aria-current", "page")
    expect(screen.getByRole("button", { name: /Healthy Market.*HEALTHY/ })).toHaveAttribute("aria-pressed", "true")
  })

  it("renders the healthy radar and records a manual exposure", async () => {
    const fetchMock = stubApi()
    render(<Home />)

    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Belem Sentinel USDC HEALTHY/ })).toBeInTheDocument()
    expect(screen.getByText("No active incidents.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Add exposure" }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/exposures")).toBe(true))
    expect(screen.getByRole("status")).toHaveTextContent("Exposure recorded")
  })

  it("uses a full radar workspace whose exposure and incident panels never cover each other", async () => {
    stubApi()
    render(<Home />)

    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.queryByText("Monitor dependencies, propagation, and portfolio impact")).not.toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Exposure" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Incidents" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Incidents (0)" })).toHaveAttribute("aria-controls", "incident-drawer-panel")

    // Opening one panel closes the other, so a long incident list cannot squeeze the form.
    fireEvent.click(screen.getByRole("button", { name: "Incidents (0)" }))
    expect(screen.getByRole("heading", { name: "Incidents" })).toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Hide incidents" }))
    expect(screen.queryByRole("heading", { name: "Incidents" })).not.toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Exposure" }))
    expect(screen.getByRole("region", { name: "Exposure" })).toBeInTheDocument()
  })

  it("floats an open-by-default exposure panel that keeps edited values while hidden", async () => {
    stubApi()
    render(<Home />)

    const exposure = await screen.findByRole("region", { name: "Exposure" })
    fireEvent.change(within(exposure).getByLabelText("Exposure amount"), { target: { value: "4200" } })
    fireEvent.click(screen.getByRole("button", { name: "Hide exposure" }))

    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()
    const launcher = screen.getByRole("button", { name: "Exposure" })
    expect(launcher).toHaveAttribute("aria-expanded", "false")
    expect(launcher).toHaveAttribute("aria-controls", "exposure-panel")
    expect(screen.queryByRole("heading", { name: "Incidents" })).not.toBeInTheDocument()

    fireEvent.click(launcher)
    expect(within(screen.getByRole("region", { name: "Exposure" })).getByLabelText("Exposure amount")).toHaveValue("4200")
  })

  it("keeps exposure values and the last submission across category changes", async () => {
    stubApi()
    render(<Home />)

    const exposure = await screen.findByRole("region", { name: "Exposure" })
    fireEvent.change(within(exposure).getByLabelText("Exposure amount"), { target: { value: "4200" } })
    fireEvent.click(within(exposure).getByRole("button", { name: "Add exposure" }))
    expect(await screen.findByRole("status")).toHaveTextContent("Exposure recorded")

    fireEvent.click(screen.getByRole("button", { name: "Markets" }))
    fireEvent.click(screen.getByRole("button", { name: "Graph" }))

    expect(within(screen.getByRole("region", { name: "Exposure" })).getByLabelText("Exposure amount")).toHaveValue("4200")
    expect(screen.getByRole("status")).toHaveTextContent("Exposure recorded")
  })

  it("starts graph-first with both panels closed on narrow screens", async () => {
    stubApi()
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("max-width: 48rem"),
      media: query,
      onchange: null,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
      dispatchEvent: () => false,
    }))
    render(<Home />)

    // Graph first: both near-full-width panels start closed.
    expect(await screen.findByRole("button", { name: "Exposure" })).toHaveAttribute("aria-expanded", "false")
    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Incidents" })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Exposure" }))
    fireEvent.click(screen.getByRole("button", { name: "Incidents (0)" }))
    expect(screen.getByRole("heading", { name: "Incidents" })).toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "Exposure" })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "Exposure" }))
    expect(screen.getByRole("region", { name: "Exposure" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Incidents" })).not.toBeInTheDocument()
  })

  it("hands a completed analysis off to the focused graph and keeps it on return", async () => {
    const analyzedMarket = {
      entity_id: "market:1:wsteth-weth", entity_type: "market", chain_id: 1,
      address_or_key: "0xB8FC70E82BC5BB53E773626FCC6A23F7EEFA036918D7EF216ECFB1950A94A85E",
      label: "wstETH / WETH radar market", source: "fixture:radar", catalog: true, visible: true,
      metadata: { health_status: "WARNING" },
    }
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph"
        ? Response.json({ ...radarGraph, entities: [...radarGraph.entities, analyzedMarket] })
        : base(url)
    )))
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    const recommendation = await screen.findByRole("region", { name: "Recommendation" })
    fireEvent.click(within(recommendation).getByRole("button", { name: "View dependencies" }))

    expect(screen.getByRole("button", { name: "Graph" })).toHaveAttribute("aria-current", "page")
    const detail = await screen.findByRole("region", { name: "Selected radar entity" })
    expect(within(detail).getByRole("heading", { name: "wstETH / WETH radar market" })).toBeInTheDocument()

    openPositionAnalysis()
    expect(screen.getByRole("region", { name: "Recommendation" })).toBeInTheDocument()
  })

  it("keeps an analysis that completes while another category is open", async () => {
    let release: ((response: Response) => void) | undefined
    stubApi(() => new Promise<Response>((resolve) => { release = resolve }))
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    await waitFor(() => expect(release).toBeDefined())
    fireEvent.click(screen.getByRole("button", { name: "Graph" }))
    await act(async () => release?.(Response.json(analysisResponse)))

    openPositionAnalysis()
    expect(await screen.findByRole("region", { name: "Recommendation" })).toBeInTheDocument()
  })

  it("runs the select → inputs → scenarios → result flow", async () => {
    const fetchMock = stubApi()
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    fireEvent.change(screen.getByLabelText("Collateral shock severity (%)"), { target: { value: "15" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    const recommendation = await screen.findByRole("region", { name: "Recommendation" })
    expect(within(recommendation).getByRole("heading", { name: "EXIT" })).toBeInTheDocument()

    const [, init] = fetchMock.mock.calls.find(([url]) => url === "/api/analysis") as unknown as [
      string,
      RequestInit,
    ]
    const body = JSON.parse(String(init.body))
    expect(body.position).toMatchObject({ mode: "borrower", entity_id: "wsteth-weth-965", ltv: "0.9" })
    expect(body.scenarios).toHaveLength(5)

    fireEvent.click(within(recommendation).getByRole("button", { name: "wstETH / WETH market" }))
    const detail = screen.getByRole("region", { name: "Selected dependency" })
    expect(within(detail).getByRole("heading", { name: "wstETH / WETH market" })).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Data quality" })).toBeInTheDocument()
  })

  it("drops an in-flight analysis when the entry changes before it resolves", async () => {
    let resolveAnalysis: (response: Response) => void = () => {}
    stubApi(() => new Promise<Response>((resolve) => (resolveAnalysis = resolve)))
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /wstETH collateral \/ WETH debt/ }))
    fireEvent.click(screen.getByRole("radio", { name: "Borrower" }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(screen.getByRole("button", { name: "Running analysis…" })).toBeDisabled()

    fireEvent.click(screen.getByRole("radio", { name: /Belem RWA EURCV/ }))
    resolveAnalysis(Response.json(analysisResponse))
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(screen.queryByRole("region", { name: "Recommendation" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Run analysis" })).toBeEnabled()
  })

  it("resets scenario edits and errors when the entry changes", async () => {
    stubApi()
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.change(screen.getByLabelText("Stablecoin depeg severity (%)"), { target: { value: "200" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))
    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveAccessibleDescription(
      "Enter a severity above 0% and at most 100%",
    )

    fireEvent.click(screen.getByRole("radio", { name: /MEV Capital wETH/ }))

    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveValue("10")
    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).not.toHaveAttribute("aria-invalid")
  })

  it("reports scenario errors even when the position is also invalid", async () => {
    const fetchMock = stubApi()
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "abc" } })
    fireEvent.change(screen.getByLabelText("Stablecoin depeg severity (%)"), { target: { value: "200" } })
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(screen.getByLabelText("Stablecoin depeg severity (%)")).toHaveAccessibleDescription(
      "Enter a severity above 0% and at most 100%",
    )
    expect(screen.getByLabelText("Amount")).toHaveAccessibleDescription("Enter a positive number")
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analysis")).toHaveLength(0)
  })

  it("shows a sanitized error when the catalog cannot load", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "Risk service unavailable" }, { status: 502 })))
    render(<Home />)

    expect(await screen.findByRole("alert")).toHaveTextContent("Risk service unavailable")
  })

  it("shows analysis errors without a recommendation", async () => {
    stubApi(Response.json({ error: "The risk service rejected this position or scenario selection" }, { status: 422 }))
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The risk service rejected this position or scenario selection",
    )
    expect(screen.queryByRole("region", { name: "Recommendation" })).not.toBeInTheDocument()
  })

  it("blocks analysis when no scenario is selected", async () => {
    const fetchMock = stubApi()
    render(<Home />)
    openPositionAnalysis()

    fireEvent.click(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }))
    for (const checkbox of screen.getAllByRole("checkbox")) fireEvent.click(checkbox)
    fireEvent.click(screen.getByRole("button", { name: "Run analysis" }))

    expect(screen.getByText("Select at least one scenario")).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analysis")).toHaveLength(0)
  })

  it("shows CONNECTING rather than LIVE until the catalog and radar have loaded", async () => {
    stubApi()
    render(<Home />)

    expect(screen.getByText("CONNECTING")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
    expect(await screen.findByText("LIVE")).toBeInTheDocument()
  })

  it("reports a radar outage as DEGRADED", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json({ error: "Risk service unavailable" }, { status: 502 }) : base(url)
    )))
    render(<Home />)

    expect(await screen.findByText("DEGRADED")).toBeInTheDocument()
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument()
  })

  it("announces a failed exposure as an alert, not as a success status", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/exposures" ? Response.json({ error: "The risk service rejected this exposure" }, { status: 422 }) : base(url)
    )))
    render(<Home />)

    fireEvent.click(await screen.findByRole("button", { name: "Add exposure" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("The risk service rejected this exposure")
    expect(screen.queryByText("Exposure recorded", { exact: false })).not.toBeInTheDocument()
  })

  it("coalesces a burst of radar events into a single graph refetch", async () => {
    const sources: { emit: (type: string, payload: unknown) => void }[] = []
    vi.stubGlobal("EventSource", class {
      listeners = new Map<string, (event: MessageEvent) => void>()
      constructor() { sources.push(this) }
      addEventListener(type: string, listener: (event: MessageEvent) => void) { this.listeners.set(type, listener) }
      close() {}
      emit(type: string, payload: unknown) { this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(payload) })) }
    })
    const fetchMock = stubApi()
    const graphCalls = () => fetchMock.mock.calls.filter(([url]) => url === "/api/graph").length
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })
    expect(graphCalls()).toBe(1)

    act(() => {
      for (const source of sources) {
        source.emit("graph.updated", { sequence: 1, type: "graph.updated", resource_id: "graph", summary: {} })
        source.emit("incident.opened", { sequence: 2, type: "incident.opened", resource_id: "incident:a", summary: {} })
        source.emit("graph.updated", { sequence: 3, type: "graph.updated", resource_id: "graph", summary: {} })
        source.emit("incident.updated", { sequence: 4, type: "incident.updated", resource_id: "incident:a", summary: {} })
      }
    })

    await waitFor(() => expect(graphCalls()).toBe(2))
    await new Promise((resolve) => setTimeout(resolve, 600))
    expect(graphCalls()).toBe(2)
  })

  it("keeps the most recently selected incident when an older response arrives late", async () => {
    const incidentFor = (id: string, label: string) => ({
      incident_id: id, source_entity_id: "vault:1:catalog", severity: "CRITICAL", status: "OPEN",
      trigger_metrics: {}, rule_version: "v1", first_observed_at: "2026-10-10T12:00:00Z",
      last_observed_at: "2026-10-10T12:00:00Z", resolved_at: null, reasons: [label],
    })
    const graphWithIncidents = {
      ...radarGraph,
      entities: [radarGraph.entities[0], { ...radarGraph.entities[1], entity_id: "vault:1:peer" }],
      active_incidents: [incidentFor("incident:a", "Alpha"), { ...incidentFor("incident:b", "Beta"), source_entity_id: "vault:1:peer" }],
    }
    let releaseA: (() => void) | undefined
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/graph") return Response.json(graphWithIncidents)
      if (url.startsWith("/api/graph?incident_graph_id=")) {
        const id = decodeURIComponent(url.split("=")[1])
        const body = {
          schema_version: "1.0.0", incident: graphWithIncidents.active_incidents.find((item) => item.incident_id === id),
          entities: [], relationships: [], similarities: [], exposures: [], impacts: [], generated_at: "2026-10-10T12:00:00Z",
        }
        if (id === "incident:a") await new Promise<void>((resolve) => { releaseA = resolve })
        return Response.json(body)
      }
      return base(url)
    }))
    render(<Home />)
    await openIncidentDrawer()
    const rail = await screen.findByRole("list", { name: "Active incidents" })
    const button = (name: string) => within(rail).getByRole("button", { name })

    fireEvent.click(button("Belem Sentinel USDC CRITICAL"))
    fireEvent.click(button("Institutional USDC Peer CRITICAL"))
    await waitFor(() => expect(button("Institutional USDC Peer CRITICAL")).toHaveAttribute("aria-pressed", "true"))
    await waitFor(() => expect(releaseA).toBeDefined())
    await act(async () => releaseA?.())

    expect(button("Institutional USDC Peer CRITICAL")).toHaveAttribute("aria-pressed", "true")
    expect(button("Belem Sentinel USDC CRITICAL")).toHaveAttribute("aria-pressed", "false")
  })

  it("keeps the latest incident chosen from the Incidents category when an older response arrives late", async () => {
    const incidentFor = (id: string, source: string, label: string) => ({
      incident_id: id, source_entity_id: source, severity: "CRITICAL", status: "OPEN",
      trigger_metrics: {}, rule_version: "v1", first_observed_at: "2026-10-10T12:00:00Z",
      last_observed_at: "2026-10-10T12:00:00Z", resolved_at: null, reasons: [label],
    })
    const graphWithIncidents = {
      ...radarGraph,
      active_incidents: [incidentFor("incident:a", "vault:1:catalog", "Alpha"), incidentFor("incident:b", "vault:1:peer", "Beta")],
    }
    let releaseA: (() => void) | undefined
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/graph") return Response.json(graphWithIncidents)
      if (url.startsWith("/api/graph?incident_graph_id=")) {
        const id = decodeURIComponent(url.split("=")[1])
        if (id === "incident:a") await new Promise<void>((resolve) => { releaseA = resolve })
        return Response.json({
          schema_version: "1.0.0", incident: graphWithIncidents.active_incidents.find((item) => item.incident_id === id),
          entities: [], relationships: [], similarities: [], exposures: [], impacts: [], generated_at: "2026-10-10T12:00:00Z",
        })
      }
      return base(url)
    }))
    render(<Home />)
    await screen.findByRole("heading", { name: "Morpho radar graph" })
    const chooseFromCategory = (label: string) => {
      // Scoped queries: whole-document role lookups are slow on this large DOM.
      fireEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Incidents" }))
      const list = screen.getByRole("list", { name: "Active incident categories" })
      fireEvent.click(within(list).getByRole("button", { name: new RegExp(label) }))
    }

    chooseFromCategory("Belem Sentinel USDC")
    await waitFor(() => expect(releaseA).toBeDefined())
    chooseFromCategory("Institutional USDC Peer")
    await openIncidentDrawer()
    const rail = screen.getByRole("list", { name: "Active incidents" })
    await waitFor(() => expect(within(rail).getByRole("button", { name: "Institutional USDC Peer CRITICAL" })).toHaveAttribute("aria-pressed", "true"))
    await act(async () => releaseA?.())

    expect(within(rail).getByRole("button", { name: "Institutional USDC Peer CRITICAL" })).toHaveAttribute("aria-pressed", "true")
    expect(within(rail).getByRole("button", { name: "Belem Sentinel USDC CRITICAL" })).toHaveAttribute("aria-pressed", "false")
  })

  it("shows loading skeletons until the catalog and radar arrive", async () => {
    stubApi()
    render(<Home />)

    expect(screen.getByText("Loading radar topology…")).toBeInTheDocument()
    openPositionAnalysis()
    expect(screen.getByText("Loading curated catalog…")).toBeInTheDocument()
    await screen.findByRole("radio", { name: /Belem RWA EURCV/ })
    expect(screen.queryByText("Loading curated catalog…")).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Graph" }))
    expect(await screen.findByRole("heading", { name: "Morpho radar graph" })).toBeInTheDocument()
    expect(screen.queryByText("Loading radar topology…")).not.toBeInTheDocument()
  })

  it("explains an empty radar instead of drawing an empty graph", async () => {
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (
      url === "/api/graph" ? Response.json({ ...radarGraph, entities: [] }) : base(url)
    )))
    render(<Home />)

    expect(await screen.findByText("Radar has no data yet — discovery runs hourly")).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Exposure" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Add exposure" })).toBeDisabled()
  })

  it("retries the catalog and radar with backoff while the risk service is busy", async () => {
    const base = stubApi()
    let busyCatalog = 1
    let busyGraph = 2
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/catalog" && busyCatalog-- > 0) {
        return Response.json({ error: "Risk service is busy — retrying" }, { status: 503 })
      }
      if (url === "/api/graph" && busyGraph-- > 0) {
        return Response.json({ error: "Risk service is starting or unreachable" }, { status: 503 })
      }
      return base(url)
    })
    vi.stubGlobal("fetch", fetchMock)
    render(<Home />)

    openPositionAnalysis()
    // Retried failures are announced politely and never as a dead-end alert.
    expect(await screen.findByText("Risk service is busy — retrying")).toHaveAttribute("role", "status")
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByText("Loading curated catalog…")).toBeInTheDocument()
    expect(screen.getByText("Loading radar topology…")).toBeInTheDocument()

    expect(await screen.findByRole("radio", { name: /Belem RWA EURCV/ }, { timeout: 4_000 })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Graph" }))
    // The radar is still retrying once the catalog error is gone.
    expect(
      await screen.findByText("Risk service is starting or unreachable — retrying automatically", {}, { timeout: 4_000 }),
    ).toHaveAttribute("role", "status")
    expect(await screen.findByRole("button", { name: /Belem Sentinel USDC HEALTHY/ }, { timeout: 6_000 })).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/catalog")).toHaveLength(2)
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/graph")).toHaveLength(3)
    expect(screen.queryByText(/retrying/)).not.toBeInTheDocument()
    expect(await screen.findByText("LIVE")).toBeInTheDocument()
  }, 15_000)

  it("does not retry a non-retryable failure", async () => {
    const fetchMock = vi.fn(async (_url: string) => Response.json({ error: "Risk service unavailable" }, { status: 502 }))
    vi.stubGlobal("fetch", fetchMock)
    render(<Home />)

    expect(await screen.findByRole("alert")).toHaveTextContent("Risk service unavailable")
    await new Promise((resolve) => setTimeout(resolve, 1_500))
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/catalog")).toHaveLength(1)
  })

  it("merges the selected incident's graph so path nodes missing from the pruned radar are drawn", async () => {
    const incident = {
      incident_id: "incident:oracle", source_entity_id: "oracle:1:hidden", severity: "CRITICAL", status: "OPEN",
      trigger_metrics: {}, rule_version: "v1", first_observed_at: "2026-10-10T12:00:00Z",
      last_observed_at: "2026-10-10T12:00:00Z", resolved_at: null, reasons: ["Oracle deviated."],
    }
    const oracle = { ...radarGraph.entities[0], entity_id: "oracle:1:hidden", entity_type: "oracle", catalog: false, visible: false, label: "Pruned Oracle", metadata: {} }
    const market = { ...radarGraph.entities[0], entity_id: "market:1:pruned", entity_type: "market", catalog: false, visible: false, label: "Pruned Market", metadata: {} }
    const base = stubApi()
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url === "/api/graph") return Response.json({ ...radarGraph, active_incidents: [incident] })
      if (url.startsWith("/api/graph?incident_graph_id=")) {
        return Response.json({
          schema_version: "1.0.0", incident, entities: [oracle, market],
          relationships: [
            { source_entity_id: "market:1:pruned", target_entity_id: "oracle:1:hidden", relationship_type: "uses_oracle", weight: null, metadata: {} },
            { source_entity_id: "vault:1:catalog", target_entity_id: "market:1:pruned", relationship_type: "allocated_to", weight: "0.5", metadata: {} },
          ],
          similarities: [], exposures: [],
          impacts: [{
            incident_id: "incident:oracle", target_entity_id: "vault:1:catalog", path: ["oracle:1:hidden", "market:1:pruned", "vault:1:catalog"],
            distance: 2, confidence: "0.9", estimated_affected_amount: null, reasons: [], revealed: false, alternate_path_count: 0,
          }],
          generated_at: "2026-10-10T12:00:00Z",
        })
      }
      return base(url)
    }))
    render(<Home />)

    await openIncidentDrawer()
    const rail = await screen.findByRole("list", { name: "Active incidents" })
    expect(screen.queryByRole("button", { name: /Pruned Market/ })).not.toBeInTheDocument()
    // The pruned radar lacks the source entity, so the rail falls back to its id until merged.
    fireEvent.click(within(rail).getByRole("button", { name: "oracle:1:hidden CRITICAL" }))
    expect(await within(rail).findByRole("button", { name: "Pruned Oracle CRITICAL" })).toHaveAttribute("aria-pressed", "true")

    // The node list, not the incident drawer floating over the same graph region.
    const nodes = screen.getByRole("list", { name: "Radar nodes" })
    expect(await within(nodes).findByRole("button", { name: /Pruned Market/ })).toHaveAttribute("data-incident-path", "true")
    expect(within(nodes).getByRole("button", { name: /Pruned Oracle CRITICAL/ })).toHaveAttribute("data-incident-path", "true")
    expect(within(screen.getByRole("list", { name: "Radar relationships" })).getByText(/Pruned Market — uses oracle → Pruned Oracle/)).toBeInTheDocument()
  })

  it("fetches and shows an entity's neighbours on request", async () => {
    const hidden = { ...radarGraph.entities[1], entity_id: "vault:1:hidden", label: "Hidden Neighbour Vault", visible: false }
    const market = { ...radarGraph.entities[1], entity_id: "market:1:shared", entity_type: "market", label: "Shared Market", visible: false }
    const base = stubApi()
    const fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/graph?focus=vault%3A1%3Acatalog") {
        return Response.json({
          ...radarGraph,
          entities: [radarGraph.entities[0], market, hidden],
          relationships: [
            { source_entity_id: "vault:1:catalog", target_entity_id: "market:1:shared", relationship_type: "allocated_to", weight: null, metadata: {} },
            { source_entity_id: "vault:1:hidden", target_entity_id: "market:1:shared", relationship_type: "allocated_to", weight: null, metadata: {} },
          ],
        })
      }
      return base(url)
    })
    vi.stubGlobal("fetch", fetchMock)
    render(<Home />)

    fireEvent.click(await screen.findByRole("button", { name: /Belem Sentinel USDC HEALTHY/ }))
    const detail = screen.getByRole("region", { name: "Selected radar entity" })
    fireEvent.click(within(detail).getByRole("button", { name: "Show neighbours" }))

    const nodes = screen.getByRole("list", { name: "Radar nodes" })
    expect(await within(nodes).findByRole("button", { name: /Hidden Neighbour Vault/ })).toBeInTheDocument()
    expect(within(nodes).getByRole("button", { name: /Shared Market/ })).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Selected radar entity" })).getByRole("button", { name: "Neighbours shown" })).toBeDisabled()
    expect(fetchMock.mock.calls.filter(([url]) => url.includes("focus="))).toHaveLength(1)
  })
})
